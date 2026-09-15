#!/usr/bin/env python3
"""KF1950 bitLode replay driven by the RE-ARM CYCLE (datasheet §8.6), not bare jobs.

Why this exists
---------------
`kf1950_wm_init_job.py` sends init + a job stream and gets ZERO bytes back. §8.6 predicts
exactly that: "Replays that send init + bare jobs decay within seconds (0 RX); replays that
loop the re-arm cycle sustain 130-280 KB of RX per cycle indefinitely." Core-enable is NOT
one-shot -- the full 63x6 round has to repeat every cycle.

Two deliberate choices
----------------------
1. JOBS ARE REAL, CAPTURED WM JOBS (tools/jobpool/*.bin), replayed verbatim. The datasheet
   carries an explicit ENCODER TRAP warning (§9.1): a wrong ctx.state->wire transform is
   silent and total -- every field valid, every CRC good, no nonce ever verifies. Replaying
   the WM's own bytes cannot hit that trap, so a null here is not an encoder artifact.
   ⚠ The work is stale (old ntime/merkle). That is fine for "does the chain emit anything";
   it is NOT fine for pool submission.
2. Baud is followed to FAST_BAUD after burst1 -- see kf1950_wm_init_job.py for why that
   matters (the chain switches during burst1 and this toolchain never followed it).

Standing rule: the caller controls hashboard power. This script does NOT switch power.
"""
import time, sys, os
from collections import Counter
import serial, struct

import kf1950_wm_init_job as wm

POOL = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jobpool')
import os as _os
CYCLE_S      = 2.67     # §8.6: one re-arm cycle per ~2.67 s, same period as the freq step
# Job load per cycle. §8.6 observes ~102 X81 + ~47 legacy on the WM's wire; both are
# env-overridable so return-path traffic can be varied at FIXED frequency, which is the
# only way to test whether relay depth contracts with congestion (§0.2n).
N_X81        = int(_os.environ.get('KF_N_X81', '102'))
N_LEGACY     = int(_os.environ.get('KF_N_LEG', '47'))
TICKET_N     = int(_os.environ.get('KF_TICKET_N', '4'))  # 2^4 = 16. nini's recommended POSITIVE CONTROL: ESP-Miner's own
                        # self_test.c drops the mask to `#define DIFFICULTY 16` specifically
                        # to force a nonce flood. 8 (=256) is the production default.
RUN_S        = float(_os.environ.get('KF_RUN_S', '210'))    # onset on known-good silicon is 14-33 s; leave generous headroom
# ⚠⚠ [REWRITTEN 2026-09-04] The old "PLL_RAMP" stepped fbdiv +1 per cycle inside the
# `03 14 06 00 [fb] 04 05 [domain] AA` frame. That is the PLL DOMAIN-PROGRAMMING form, NOT the
# frequency form -- so it was never a frequency command and the earlier ramp-on/ramp-off A/B
# compared two arms that BOTH ramped nothing. §7.3's frequency form is
#     03 14 06 [fbHi] [fbLo] 04 [post_div_code] 03 AA
# and §7.3 names the exact failure we were committing as the ROOT CAUSE of every failed
# bitLode replay: ramping fbdiv while holding post_div fixed at 2 makes VCO = freq*4 exceed
# the ~1200 MHz PLL maximum past ~300 MHz, so it CANNOT LOCK. post_div MUST step 2->1->0.
# The WM recomputes fbdiv from the target frequency every step; it does not increment it.
# --- runtime overrides so an A/B arm needs no file edit (env vars) ------------
#   KF_TICKET_N   ticket exponent (4 = diff 16 flood, 32 = parked/report-nothing)
#   KF_PLL_MODE   ramp | fixed | domain     ('domain' reproduces the 2026-09-04 DEFECT)
#   KF_FREQ       fixed frequency in MHz for KF_PLL_MODE=fixed
#   KF_RUN_S      run length in seconds
#   KF_TAG        label written into the RX filename
PLL_MODE     = _os.environ.get('KF_PLL_MODE', 'ramp')
PLL_PROBE    = _os.environ.get('KF_PLL_PROBE', '') == '1'
RESTORE_CE   = _os.environ.get('KF_RESTORE_CE', '') == '1'   # put reg 0x042 back to 0xB1
TAG          = _os.environ.get('KF_TAG', 'run')
FIXED_FREQ   = int(_os.environ.get('KF_FREQ', '198'))
# --- 2026-09-10 EVEN-PACING A/B -------------------------------------------------
# The stock WM emits ONE job every 5.0 ms (199.7 j/s, measured on the wire between
# ASIC 0 and 1) and leaves the downstream line idle ~92 % of the time. This replay
# instead writes a whole cycle's jobs as ONE saturating blob. KF_PACE_HZ>0 spreads
# the job region evenly at that rate instead. Bytes and their ORDER are unchanged --
# only the timing differs -- so an A/B isolates traffic SHAPE alone.
PACE_HZ      = float(_os.environ.get('KF_PACE_HZ', '0'))
CE_FULL      = _os.environ.get('KF_CE_FULL', '') == '1'
# --- SOLO mode: make exactly ONE chip hash and report ---------------------------
# KF_SOLO=<hex addr>. Emulates a single-ASIC board on our 111-chip chain so the
# per-chip job-rate figure can be measured (it is currently an INFERENCE from a
# 111-chip board -- see the N-independence caveat). Levers, strongest first:
#   mask : BROADCAST park + ADDRESSED unpark -> only this chip can return anything
#          (SS0.2k: a parked mask gives 0 bytes total, echoes included)
#   CE   : core-enable + latch addressed -> only this chip keeps cores armed
#   jobs : addressed -> only this chip ingests (T4.3: addressed chip dominates ~40x)
#   base : a single nonce-base record, base 0, so it owns the whole 2^32
# PLL stays BROADCAST: every WM PLL write ever captured is broadcast, and addressed
# PLL writes are untested. The other chips therefore still clock and draw power --
# this is one hashing ASIC in a live chain, NOT an electrically isolated chip.
# ⚠ It CANNOT test the "no neighbour" question: chip 1 is still present and still
# drives CTSI at the ASIC0<->1 link.
SOLO         = _os.environ.get('KF_SOLO', '')
# --- RST experiments (docs/CONTROL_SIGNAL_TEST_PLAN_20260914.md §3a) -----------------
# KF_NO_RESET=1        never touch RESETN (bitLode GPIO11 idles HIGH) -> tests R1/R2
# KF_RESET_MS=<ms>     pulse width; raw control packets, bypassing send_ctrl's 0.3 s sleep -> R3
# KF_RST_HOLD_AT=<n>   assert RESETN before cycle n (1-based) for KF_RST_HOLD_S s, release,
#                      then keep sending jobs WITHOUT re-init -> R4 negative control
NO_RESET     = _os.environ.get('KF_NO_RESET', '') == '1'
RESET_MS     = _os.environ.get('KF_RESET_MS', '')
RST_HOLD_AT  = int(_os.environ.get('KF_RST_HOLD_AT', '0'))
RST_HOLD_S   = float(_os.environ.get('KF_RST_HOLD_S', '5'))
SOLO_ADDR    = int(SOLO, 16) if SOLO else None   # replay the WM's real 1008-frame sweep   # 0 = original blob write
PLL_RAMP     = (PLL_MODE == 'ramp')
FREQ_START   = 198      # §7.3 ramp law: two-phase, ALWAYS +6 MHz per step
FREQ_STEP    = 6
FREQ_TARGET  = int(_os.environ.get('KF_FREQ_TARGET', '618'))
                        # crosses both pd boundaries (2->1 at 300, 1->0 at 600).
                        # 2026-09-10: the STOCK controller was measured holding a
                        # healthy board at 800 MHz (773 M effective, P 96.6%, F 0),
                        # so 618 is NOT the ceiling -- but ONLY reachable by ramping
                        # (SS0.2l: a JUMP to 618 kills the chain, a ramp to it is fine).
REPEAT_BATCH = _os.environ.get('KF_REPEAT_BATCH', '1') != '0'
                        # resend the IDENTICAL job batch every cycle. DECISIVE TEST: if the
                        # echoes repeat, the chain is alive and the canned-bank echo simply
                        # doesn't re-fire for a job it has already recognised (our expectation
                        # was wrong). If they don't, the chain really does change state in
                        # cycle 1. A/B'd: ramp on/off and arm-round made NO difference.
CE_ARM_FIRST = True     # send the 0x31 ARM round before the 0xB1 COMMIT round, as burst0 does.
                        # §8.6 lists only the B1 round in the re-arm cycle, but §8.7's driver
                        # rule is "send the whole sweep on every re-arm cycle" -- and a
                        # commit-without-arm is the leading suspect for the after-cycle-1 decay.
FBDIV_START  = 0x80
# ⚠⚠ [CORRECTED 2026-09-04] Was False on the reading that §7.2b's sweep is a one-time
# post-enumeration step. IT IS NOT. A LEGACY JOB IS A 222-BYTE BROADCAST WRITE TO reg 0x800
# (`FF FF 80 04 DE ...` -> reg 0x800, access 4, len 0xDE), and the per-chip nonce base lives
# at 0x800-0x801 -- so EVERY legacy job overwrites it with job bytes. Confirmed on the wire:
# 3 s of WM steady state contains 426 broadcast 0x800 job writes AND 111 addressed 0x800
# sweep records. The WM re-sends the sweep continuously. Bench-confirmed too: after a burst
# of X81-only jobs (which write 0x810, not 0x800) the base is untouched, 0/111 changed.
NONCE_BASE_EACH_CYCLE = True


# --- T2.2 defect re-introduction + T3.4 fresh work ---------------------------
#   KF_JOBS=fresh        use jobpool/fresh_x81.bin (rolled merkle tails = new nonce space)
#   KF_NO_BAUD_FOLLOW=1  defect 1: do not follow the chain to FAST_BAUD after burst1
#   KF_SLOW_BAUD=363636  defect 2: use the wrong 12MHz/33 divisor
#   KF_NO_UNPARK=1       defect 3: leave reg 0x051 parked at 2^32
USE_FRESH   = _os.environ.get('KF_JOBS', '') == 'fresh'
NO_BAUD_FOLLOW = _os.environ.get('KF_NO_BAUD_FOLLOW', '') == '1'
NO_UNPARK   = _os.environ.get('KF_NO_UNPARK', '') == '1'
if _os.environ.get('KF_SLOW_BAUD'):
    wm.INIT_BAUD = int(_os.environ['KF_SLOW_BAUD'])


def load_pool():
    def chunks(path, n):
        b = open(path, 'rb').read()
        assert len(b) % n == 0, f"{path}: {len(b)} not a multiple of {n}"
        return [b[i:i+n] for i in range(0, len(b), n)]
    x = chunks(os.path.join(POOL, 'fresh_x81.bin' if USE_FRESH else 'x81_jobs.bin'), 212)
    l = chunks(os.path.join(POOL, 'legacy_jobs.bin'), 228)
    for j in x + l:
        assert wm.kf1950_crc(j[:-1]) == j[-1], "job pool CRC failure"
    return x, l


def nonce_base_sweep(n_chips=111):
    """§7.2b -- give every chip its own 1/111 slice of the 32-bit nonce space.

    111 addressed 8-byte records, RAW (no FF FF preamble), own CRC init 0xFF:

        [addr] 00 80 04 02 [V_hi] [V_lo] [CRC]
        V16 = (addr * 2**32 // n_chips) >> 16

    ⚠ THE WM SENDS THIS AND THIS TOOLCHAIN NEVER DID -- not in burst0, not in burst1, not in
    the re-arm cycle. Without it chips have no assigned search window. The WM sends it in the
    FAST phase, immediately after enumeration, so it goes after the baud switch.

    Verified 111/111 byte-exact against the 2026-09-03 capture (bringup_fast), including the
    datasheet's stated endpoint addr 0x6E -> V16 = 0xFDB1.
    """
    out = bytearray()
    for addr in range(n_chips):
        v = (addr * (1 << 32) // n_chips) >> 16
        body = bytes([addr, 0x00, 0x80, 0x04, 0x02, (v >> 8) & 0xFF, v & 0xFF])
        out += body + bytes([wm.kf1950_crc(body)])
    return bytes(out)


def pll_freq(mhz):
    """§7.3 frequency-set frame. VALIDATED against all 11 documented (freq, fbdiv, pd) cases
    and all 6 literal frames in §7.3, byte-exact.

        post_div_code pd = 3 if f<125, 2 if f<300, 1 if f<600, 0 if f>=600   (VCO guard)
        fbdiv            = round(2**pd * f / 6)
        f_MHz            = fbdiv * 24 / (4 * 2**pd)      VCO = fbdiv * 6 (must stay ~600-1200)
    """
    pd = 3 if mhz < 125 else 2 if mhz < 300 else 1 if mhz < 600 else 0
    fb = round((1 << pd) * mhz / 6)
    return wm.make_pkt(bytes([0x03, 0x14, 0x06, (fb >> 8) & 0xFF, fb & 0xFF,
                              0x04, pd, 0x03, 0xAA])), fb, pd


def addressed(frame, addr):
    """Rewrite a broadcast frame as addressed: FF FF -> [addr] 00, recompute CRC."""
    body = bytes([addr, 0x00]) + frame[2:-1]
    return body + bytes([wm.kf1950_crc(body)])


LAST_JOB_SPAN = (0, 0, [])


def rearm_cycle(x81, legacy, ix, il, fbdiv):
    """One complete re-arm cycle, in §8.6 order. Returns (bytes, ix, il)."""
    out = bytearray()
    # -- config re-send (three static opcodes, constant across bring-up and steady state)
    out += wm.make_pkt(bytes([0x80, 0x24, 0x02, 0x00, 0x00]))            # nonce-div reset
    out += wm.make_pkt(bytes([0x80, 0x44, 0x0C, 0x00, 0x00, 0x00, 0x20,
                              0x00, 0x40, 0x00, 0x60, 0x00, 0x00, 0x00, 0x00]))
    out += wm.make_pkt(bytes([0x8D, 0xD4, 0x01, 0xAA]))                  # hash mode
    # -- jobs
    #    Record the job region so the caller can pace it (KF_PACE_HZ) without changing
    #    a single byte or reordering anything: pre = out[:js], post = out[je:].
    global LAST_JOB_SPAN
    js = len(out); _lens = []
    for _ in range(N_X81):
        f = x81[ix % len(x81)]
        if SOLO_ADDR is not None: f = addressed(f, SOLO_ADDR)
        out += f; _lens.append(len(f)); ix += 1
    for _ in range(N_LEGACY):
        f = legacy[il % len(legacy)]
        if SOLO_ADDR is not None: f = addressed(f, SOLO_ADDR)
        out += f; _lens.append(len(f)); il += 1
    LAST_JOB_SPAN = (js, len(out), _lens)
    # RESTORE the per-chip nonce base: the legacy jobs just above overwrote 0x800-0x801.
    if NONCE_BASE_EACH_CYCLE:
        if SOLO_ADDR is not None:
            # one record, base 0 -> this chip owns the whole nonce space, as a real
            # single-ASIC board would (chip_num = 1)
            body = bytes([SOLO_ADDR, 0x00, 0x80, 0x04, 0x02, 0x00, 0x00])
            out += body + bytes([wm.kf1950_crc(body)])
        else:
            out += nonce_base_sweep()
    # -- maintenance halt: park the nonce-report threshold while state is rewritten
    out += wm.ticket_mask_frame(32)                                      # 05 14 04 ff ff ff ff
    # -- FULL core-enable round: 63 core-group indices x 6. NOT one-shot (§8.7).
    if CE_FULL:
        # ★ [2026-09-10] THE WM'S ACTUAL SWEEP, read back out of its own init capture
        # (vendor/KF1950_Comms-main/Initialization/EpTxdInitializationHex.csv, 1008 CRC-valid
        # `04 04 04` frames). Our sweep was wrong in EVERY dimension:
        #   indices : WM sweeps 0x01..0x70 CONTIGUOUSLY = 112. WM_CE_CHIPS has 63 -- the
        #             "banks" 0x10-0x20 / 0x31-0x40 / 0x51-0x60 it skips DO NOT EXIST in the
        #             WM stream; the "= 63 chips" comment on that tuple is simply wrong.
        #   values  : WM uses SIX (0x31 0x21 0x11 0x91 0xA1 0xB1); we sent two.
        #   order   : WM is VALUE-outer / index-inner; ours was index-outer.
        #   repeats : 0x31/0x21/0x11 once each, 0x91/0xA1/0xB1 TWICE = 9 per index.
        # Coverage of our old sweep = 63/112 x 2/6 = 18.8 %, against a MEASURED core
        # utilisation of 14.9 % -- which is why this is the prime suspect for the ~6.7x gap.
        for val, passes in ((0x31, 1), (0x21, 1), (0x11, 1),
                            (0x91, 2), (0xA1, 2), (0xB1, 2)):
            for _ in range(passes):
                for nn in range(0x01, 0x71):
                    _f = wm.make_pkt(bytes([0x04, 0x04, 0x04, nn, 0x3F, val, 0xAA]))
                    out += addressed(_f, SOLO_ADDR) if SOLO_ADDR is not None else _f
    else:
        if CE_ARM_FIRST:
            for nn in wm.WM_CE_CHIPS:
                for _ in range(6):
                    out += wm.make_pkt(bytes([0x04, 0x04, 0x04, nn, 0x3F, 0x31, 0xAA]))
        for nn in wm.WM_CE_CHIPS:
            for _ in range(6):
                out += wm.make_pkt(bytes([0x04, 0x04, 0x04, nn, 0x3F, 0xB1, 0xAA]))
    # -- latch 0 -> 1
    #    ⚠ `04 24` decodes to reg 0x042, which is the THIRD BYTE of the core-enable field --
    #    its value byte, 0xB1 on a hashing chain. So this pair leaves the chip holding
    #    `70 3f 01`, not `70 3f b1`. Found by nini (round 51) by diffing against the value we
    #    published; confirmed on our own chain: after init only we read `70 3f b1`, but after
    #    any re-arm cycle we read `70 3f 01`. ⚠ Our round-48 "core-enable matches" diff compared
    #    an init-only read against the WM's steady state -- different bring-up stages, which is
    #    exactly the comparison the datasheet warns against.
    for _v in (0x00, 0x01):
        _f = wm.make_pkt(bytes([0x04, 0x24, 0x01, _v]))
        out += addressed(_f, SOLO_ADDR) if SOLO_ADDR is not None else _f
    if RESTORE_CE:
        # restore reg 0x042 to the value a hashing chain holds
        out += wm.make_pkt(bytes([0x04, 0x24, 0x01, 0xB1]))
    # -- maintenance resume: unpark so nonces can be reported
    if not NO_UNPARK:
        _f = wm.ticket_mask_frame(TICKET_N)
        out += addressed(_f, SOLO_ADDR) if SOLO_ADDR is not None else _f
    # -- one PLL FREQUENCY step. §8.6: each slow-phase ramp step IS one re-arm cycle,
    #    not a bare 03 14 write. `fbdiv` here carries the target frequency in MHz.
    if PLL_MODE == 'domain':
        # THE 2026-09-04 DEFECT, reproduced verbatim for the A/B: the PLL DOMAIN-programming
        # form with a ramping fbdiv. This is NOT a frequency write.
        fb = min(0x80 + (fbdiv - FREQ_START) // FREQ_STEP, 0xFF)
        for domain in range(4):
            out += wm.make_pkt(bytes([0x03, 0x14, 0x06, 0x00, fb, 0x04, 0x05, domain, 0xAA]))
    else:
        frame, _, _ = pll_freq(FIXED_FREQ if PLL_MODE == 'fixed' else fbdiv)
        out += frame
    return bytes(out), ix, il


def rst_raw(ctrl, level, cid):
    """Set RESETN with a raw bitLode control packet (PAGE 6, cmd 0x00) and return at once.
    wm.send_ctrl sleeps 0.3 s per call, which floors the pulse width near 300 ms."""
    payload = bytes([cid, 0x00, 0x06, 0x00, level])
    ctrl.write(struct.pack('<H', len(payload) + 2) + payload); ctrl.flush()


def main():
    x81, legacy = load_pool()
    print(f"=== KF1950 re-arm replay ===\n  job pool: {len(x81)} X81 + {len(legacy)} legacy, "
          f"all real WM frames, all CRC-valid")
    burst0, burst1 = wm.build_burst0(), wm.build_burst1()
    print(f"  profile={wm.PROFILE}  burst0={len(burst0)}B burst1={len(burst1)}B")
    print(f"  ARM: pll_mode={PLL_MODE} freq={FIXED_FREQ if PLL_MODE=='fixed' else str(FREQ_START)+'-'+str(FREQ_TARGET)}"
          f" ticket_n={TICKET_N} (diff {1<<TICKET_N}) run_s={RUN_S:.0f} tag={TAG}"
          f" jobs={'FRESH' if USE_FRESH else 'captured'}"
          f"{' NO_BAUD_FOLLOW' if NO_BAUD_FOLLOW else ''}"
          f"{' NO_UNPARK' if NO_UNPARK else ''} init_baud={wm.INIT_BAUD}"
          # [2026-09-14] SOLO/CE_FULL were NOT logged, so a run's own log could not answer
          # "was this whole-chain or one chip?" -- it had to be inferred from the nonce-base
          # record count. Same defect family as nini r45: report what the run ACTUALLY did.
          f"{' CE_FULL' if CE_FULL else ''}"
          f"{(' *** SOLO=0x%02X -- ONE CHIP ONLY ***' % SOLO_ADDR) if SOLO_ADDR is not None else ' chain=BROADCAST(all 111)'}"
          f" reset={'NONE' if NO_RESET else (RESET_MS + 'ms-raw') if RESET_MS else '1000ms-std'}"
          f"{(' RST_HOLD@cycle%d/%gs' % (RST_HOLD_AT, RST_HOLD_S)) if RST_HOLD_AT else ''}")

    # [2026-09-14] load_pool() takes ~33 s, so a capture armed at launch ends before the reset.
    # KF_RESET_DELAY gives an external capture time to arm; the marker line is what to arm on.
    _rd = float(_os.environ.get('KF_RESET_DELAY', '0'))
    print(f"  RESET_MARKER wall={time.time():.3f} resetn_low_in={_rd:.1f}s", flush=True)
    time.sleep(_rd)
    ctrl = serial.Serial(wm.CTRL_PORT, 9600, timeout=2); time.sleep(0.1)
    if NO_RESET:
        print(f"  *** NO RESET: RESETN never touched (NO_RESET wall={time.time():.3f}) ***", flush=True)
    elif RESET_MS:
        ms = float(RESET_MS)
        ctrl.reset_input_buffer()
        print(f"  RESETN_LOW wall={time.time():.3f} (raw pulse, requested {ms:g} ms)", flush=True)
        rst_raw(ctrl, 0x00, 0x05)
        time.sleep(ms / 1000.0)
        rst_raw(ctrl, 0x01, 0x09)
        time.sleep(0.3); ctrl.reset_input_buffer()               # discard both acks
    else:
        print(f"  RESETN_LOW wall={time.time():.3f}", flush=True)
        wm.send_ctrl(ctrl, 0x05, 0x00, 0x06, bytes([0x00, 0x00]))   # RESETN LOW
        time.sleep(1.0)
        wm.send_ctrl(ctrl, 0x09, 0x00, 0x06, bytes([0x00, 0x01]))   # RESETN HIGH
    print(f"  reset done, waiting 15 s (RESETN_HIGH wall={time.time():.3f})", flush=True); time.sleep(15.0)

    # ⚠ [2026-09-10] write_timeout is MANDATORY. combo700 HUNG for ~7 min at 672 MHz:
    # the chain stopped draining, the bitLode's buffer filled, and pyserial's default
    # write_timeout=None blocked forever -- the run produced no further log line and
    # only `timeout` killed it. Same family as the unbounded drain / write-without-read
    # deadlocks already on record.
    data = serial.Serial(wm.DATA_PORT, wm.INIT_BAUD, timeout=3, write_timeout=10)
    data.reset_input_buffer()
    data.write(wm.make_pkt(bytes([0x10, 0x00, 0x06]))); time.sleep(3.0)
    n = data.in_waiting
    nchips = n // 11
    print(f"  chip ID: {n}B ({nchips} chips)")
    # ⚠ [2026-09-07] A PARTIAL enumeration is worthless data, and it used to pass silently:
    # one run reported 18 chips, read reg 0x031 as 0 B and captured 0 bytes over 79 cycles,
    # yet counted as SUCCEEDED. Demand a full chain before spending 210 s on an arm.
    MIN_CHIPS = 100
    if nchips < MIN_CHIPS:
        print(f"  FATAL: partial chain -- {nchips} chips, need >= {MIN_CHIPS}.")
        data.close(); ctrl.close(); return

    data.reset_input_buffer(); data.write(burst0); time.sleep(0.5)
    print(f"  post-burst0 RX: {data.in_waiting}B"); data.reset_input_buffer()
    data.write(burst1); data.flush(); time.sleep(0.05)
    if NO_BAUD_FOLLOW:
        print(f"  >>> DEFECT ARM: staying at {wm.INIT_BAUD} (not following the chain)")
    else:
        data.baudrate = wm.FAST_BAUD
        print(f"  >>> host UART -> {wm.FAST_BAUD} baud")
    data.reset_input_buffer()
    data.write(wm.make_pkt(bytes([0x11, 0x00, 0x01]))); time.sleep(0.4)
    _stat = data.in_waiting
    _statchips = _stat // 6
    print(f"  post-burst1 STATUS @fast: {_stat}B ({_statchips} chips)")
    # ⚠ [2026-09-10] The MIN_CHIPS gate above only checks ENUMERATION. On 2026-09-10 a run
    # enumerated 110 chips (passing that gate) but only 51 answered here, because the previous
    # arm had left the chain half-dead and a soft reset does NOT recover it (only a RAIL
    # power-cycle does). It then spent 210 s producing 3,927 B and a flat zero -- which looked
    # exactly like a real negative result for the arm under test. Gate this too.
    if not NO_BAUD_FOLLOW and _statchips < MIN_CHIPS:
        print(f"  FATAL: only {_statchips} chips answered STATUS at {wm.FAST_BAUD} baud, "
              f"need >= {MIN_CHIPS}. The chain is degraded -- POWER-CYCLE THE RAIL "
              f"(echo 0 then 1 > /sys/bitmicro/power/enable); a soft reset will NOT fix it.")
        data.close(); ctrl.close(); return
    data.reset_input_buffer()

    # §7.2b nonce-space division -- the WM does this right after enumeration and this
    # toolchain never has. 111 addressed records, one per chip.
    sweep = nonce_base_sweep()
    print(f"  nonce-base sweep (§7.2b): {len(sweep)}B, 111 addressed records "
          f"-- NEVER SENT BY THIS TOOLCHAIN BEFORE")
    data.write(sweep); data.flush(); time.sleep(0.1)
    data.reset_input_buffer()

    # reg 0x031 -- what the PLL is ACTUALLY programmed to. Never once read in this project.
    def rd031(tag):
        data.reset_input_buffer()
        data.write(wm.make_pkt(bytes([0x03, 0x10, 0x06])))
        time.sleep(0.4)
        n = data.in_waiting
        r = data.read(min(n, 4096)) if n else b''
        vals = set()
        i = 0
        while i + 11 <= len(r):
            f = r[i:i+11]
            if wm.kf1950_crc(f[:-1]) == f[-1] and f[1] == 0 and f[3] == 6:
                vals.add(f[4:10].hex(' ')); i += 11
            else:
                i += 1
        print(f"    reg 0x031 {tag}: {n}B, {len(vals)} distinct -> {sorted(vals)[:3]}")
        return vals
    print("\n  PLL programmed state:")
    rd031("pre-ramp ")

    print(f"\n  Looping re-arm cycles for {RUN_S:.0f}s "
          f"(~{RUN_S/CYCLE_S:.0f} cycles, {CYCLE_S}s each)")
    # ⚠ [2026-09-07] A USB disconnect mid-run (Errno 5) killed a GOOD run at cycle 37 with
    # 725 KB captured and wrote NOTHING, because the save happened only after the loop.
    # Capture is now flushed to disk as it arrives, so a crash costs at most one cycle.
    _outdir = _os.environ.get('KF_OUT_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sessions'))
    os.makedirs(_outdir, exist_ok=True)
    out = os.path.join(_outdir, 'rx_%s.bin' % TAG)
    rxf = open(out, 'wb')
    from collections import Counter as _C
    pll_samples = _C()
    rx = bytearray(); ix = il = 0; cyc = 0
    fbdiv = FREQ_START
    t0 = time.time(); first = None; per_cycle = []
    try:
      while time.time() - t0 < RUN_S:
        if RST_HOLD_AT and cyc + 1 == RST_HOLD_AT:
            print(f"    RST_HOLD_LOW wall={time.time():.3f} (before cycle {cyc+1}, {RST_HOLD_S:g} s, no jobs)", flush=True)
            rst_raw(ctrl, 0x00, 0x05)
            th = time.time() + RST_HOLD_S
            while time.time() < th:                               # keep draining: never write without reading
                a = data.in_waiting
                if a:
                    chunk = data.read(a); rx += chunk; rxf.write(chunk); rxf.flush()
                else:
                    time.sleep(0.005)
            rst_raw(ctrl, 0x01, 0x09)
            print(f"    RST_HOLD_HIGH wall={time.time():.3f} -- continuing jobs WITHOUT re-init", flush=True)
            time.sleep(0.3); ctrl.reset_input_buffer()
        cstart = time.time()
        blob, nix, nil_ = rearm_cycle(x81, legacy, ix, il, fbdiv)
        if not REPEAT_BATCH:
            ix, il = nix, nil_
        if PACE_HZ > 0:
            # Even pacing: identical bytes, identical order, jobs spread at PACE_HZ.
            js, je, jlens = LAST_JOB_SPAN
            data.write(blob[:js])
            gap = 1.0 / PACE_HZ
            off = js; jt = time.time()
            for k, ln in enumerate(jlens):
                data.write(blob[off:off + ln]); off += ln
                # ⚠ MUST keep reading while writing -- a previous version filled the
                # bitLode's buffers and deadlocked by writing 88 KB with no reads.
                a = data.in_waiting
                if a:
                    chunk = data.read(a); rx += chunk; rxf.write(chunk); rxf.flush()
                    if first is None:
                        first = time.time() - t0
                        print(f"    *** FIRST RX at t+{first:.2f}s ***")
                due = jt + (k + 1) * gap
                while True:
                    slack = due - time.time()
                    if slack <= 0:
                        break
                    a = data.in_waiting
                    if a:
                        chunk = data.read(a); rx += chunk; rxf.write(chunk); rxf.flush()
                    else:
                        time.sleep(min(slack, 0.001))
            assert off == je, (off, je)
            data.write(blob[je:])
        else:
            data.write(blob)
        before = len(rx)
        # drain while the cycle plays out
        while time.time() - cstart < CYCLE_S:
            a = data.in_waiting
            if a:
                chunk = data.read(a)
                rx += chunk
                rxf.write(chunk); rxf.flush()
                if first is None:
                    first = time.time() - t0
                    print(f"    *** FIRST RX at t+{first:.2f}s ***")
            else:
                time.sleep(0.002)
        cyc += 1
        # nini round-50 ask: what does BYTE 6 of reg 0x031 read while the chain is HASHING?
        # Our post-run reads all show 0x00, but those happen after jobs stop -- a live lock bit
        # could have cleared. Sample it mid-flood, with work still flowing.
        if PLL_PROBE and cyc % 10 == 0:
            data.write(wm.make_pkt(bytes([0x03, 0x10, 0x06])))
            time.sleep(0.15)
            a = data.in_waiting
            r = data.read(a) if a else b''
            rx += r; rxf.write(r); rxf.flush()
            j = 0
            while j + 11 <= len(r):
                f = r[j:j+11]
                if wm.kf1950_crc(f[:-1]) == f[-1] and f[1] == 0 and f[3] == 6:
                    pll_samples[f[4:10].hex(' ')] += 1; j += 11
                else:
                    j += 1
        got = len(rx) - before
        per_cycle.append(got)
        # ⚠ [2026-09-07] was printing the ramp variable even in 'fixed' mode, so a 618 MHz
        # arm logged "198 MHz" on every line. Display only — the wire was always correct.
        shown = FIXED_FREQ if PLL_MODE == 'fixed' else fbdiv
        _, fb, pd = pll_freq(shown)
        print(f"    cycle {cyc:3d}  {shown:4d} MHz (fbdiv 0x{fb:02X} pd {pd})  "
              f"rx={got:7d}B  total={len(rx):9d}B")
        if PLL_RAMP and fbdiv < FREQ_TARGET:
            fbdiv = min(fbdiv + FREQ_STEP, FREQ_TARGET)
    except Exception as e:
        print(f"\n  ⚠ RUN ABORTED at cycle {cyc}: {type(e).__name__}: {e}")
        print(f"  capture preserved: {len(rx)} bytes already on disk")
    finally:
        rxf.close()

    if PLL_PROBE and pll_samples:
        print("\n  reg 0x031 sampled MID-RUN (jobs flowing), %d reads:" % sum(pll_samples.values()))
        for v, n in pll_samples.most_common(5):
            print("     %s   x%d   <- byte6 = %s" % (v, n, v.split()[5]))
    print(f"\n  {cyc} cycles, {len(rx)} bytes RX total")
    if not rx:
        print("  *** ZERO bytes across every cycle ***")
    else:
        print(f"  per-cycle RX: min={min(per_cycle)} max={max(per_cycle)} "
              f"median={sorted(per_cycle)[len(per_cycle)//2]}")
        print(f"  §8.6 predicts 130-280 KB/cycle when the loop is correct")
    print(f"  saved -> {out} ({len(rx)} B, flushed live)")

    # classify what came back
    if rx:
        r = bytes(rx); n11 = ok = echo = cand = 0
        for i in range(0, len(r) - 10):
            if r[i+2:i+4] == b'\x04\x06' and wm.kf1950_crc(r[i:i+10]) == r[i+10]:
                n11 += 1
                if r[i+9] >= 0xC0: echo += 1
                else: cand += 1
        print(f"  04 06 frames (CRC-OK): {n11}   echo(byte9>=C0): {echo}   "
              f"X81-id(<=BF, nonce candidates): {cand}")

    # ── Is the chain still alive at the end, or did it actually go silent? ───────
    # The echo stopping after cycle 1 is consistent with BOTH "chain died" and "a chip
    # echoes a canned job only the first time it recognises it". Register reads separate
    # them: a dead chain answers nothing.
    rd031("post-ramp")
    # ⚠ [2026-09-08] DRAIN TO QUIET FIRST. These reads run moments after a job flood and the
    # chain is still emitting `04 06`; reset_input_buffer() alone does not help because frames
    # keep arriving. On the ce_on arm the reg 0x040 read returned echo bytes instead of a
    # register reply, so the one control that mattered -- did reg 0x042 actually land? --
    # produced nothing, and the arm could only be reported as unverified.
    t_quiet = time.time() + 5.0
    while time.time() < t_quiet:
        if data.in_waiting:
            data.read(data.in_waiting); time.sleep(0.05)
        else:
            time.sleep(0.2)
            if not data.in_waiting:
                break
    data.reset_input_buffer()

    print("\n  Post-cycle liveness check @%d baud:" % data.baudrate)
    # reg 0x800 = the per-chip nonce base we wrote in the §7.2b sweep. Reading it back is
    # what turns "sending the sweep changed nothing" into "the sweep landed and changed
    # nothing" -- i.e. an actual elimination rather than an untested send.
    for reg, n, label in ((0x100, 0x06, "chip ID"), (0x051, 0x04, "ticket mask"),
                          (0x040, 0x04, "core enable"), (0x800, 0x02, "NONCE BASE (§7.2b)"),
                          (0x802, 0x02, "nonce div"), (0x24A, 0x02, "die temp")):
        b2 = (reg >> 4) & 0xFF
        b3 = ((reg & 0xF) << 4)
        data.reset_input_buffer()
        data.write(wm.make_pkt(bytes([b2, b3, n])))
        time.sleep(0.4)
        a = data.in_waiting
        r = data.read(min(a, 8192)) if a else b''
        # parse addr-FIRST replies (§11.2) rather than trusting the head of the buffer
        vals, i, rec = {}, 0, 4 + n + 1
        while i + rec <= len(r):
            f = r[i:i+rec]
            if wm.kf1950_crc(f[:-1]) == f[-1] and f[1] == 0 and f[3] == n:
                vals[f[0]] = bytes(f[4:4+n]); i += rec
            else:
                i += 1
        from collections import Counter as _C2
        c = _C2(v.hex(' ') for v in vals.values())
        top = '  '.join("%s x%d" % (k, v) for k, v in c.most_common(2)) or '(no valid reply)'
        print(f"    reg 0x{reg:03X} ({label}): {a}B raw, {len(vals)} chips parsed | {top}")

    data.write(wm.ticket_mask_frame(32)); time.sleep(0.02)
    data.close(); ctrl.close()


if __name__ == '__main__':
    main()
