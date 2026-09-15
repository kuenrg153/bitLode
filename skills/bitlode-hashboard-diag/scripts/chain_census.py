#!/usr/bin/env python3
"""Per-ASIC register census of a KF1950 M30S chain through the bitLode — no hashing, no jobs.

What it proves, chip by chip (111 expected):
  L1  enumeration @375000  : chip-ID broadcast reply count  -> chain reachable to chip N-1
  L2  STATUS @2 Mbaud      : fast-link integrity after burst0/burst1 + baud follow
  A   reg 0x100 (6 B)      : answers its OWN address, reads `19 50`
  W   reg 0x800 (2 B)      : per-chip WRITE path -- must read back hi16(addr * 2^32 / 111)
  P   reg 0x031 (6 B)      : PLL frequency frame landed (fbHi fbLo 04 pd) after a fixed-freq write
  M   reg 0x051 (4 B)      : ticket mask landed (reads back as written)
  T   reg 0x24A (2 B)      : die temperature, raw units -- judged RELATIVE to the board (outliers)
  S   reg 0x236 (2 B)      : 10-bit PVT/speed monitor -- 1023 = saturated, outliers flagged
Every read is repeated --reps times so an intermittent chip shows as a partial count.

Does NOT switch power. Leaves the ticket mask PARKED (2^32) at exit.
Needs the repo's tools/ (kf1950_wm_init_job, kf1950_rearm_replay) and the rail held by the caller.

usage: chain_census.py [--freq 198] [--reps 3] [--json OUT] [--no-reset]
"""
import sys, os, time, json, argparse, statistics
from collections import defaultdict, Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'tools'))
os.environ.setdefault('KF_N_X81', '0'); os.environ.setdefault('KF_N_LEG', '0')
import serial
import kf1950_wm_init_job as wm
import kf1950_rearm_replay as rr

N = 111


def drain(ser, quiet=0.25, cap=3.0):
    """Read until the line is quiet for `quiet` s (bounded by `cap`). Never an unbounded loop."""
    buf = bytearray(); t_end = time.time() + cap; last = time.time()
    while time.time() < t_end:
        a = ser.in_waiting
        if a:
            buf += ser.read(a); last = time.time()
        elif time.time() - last > quiet:
            break
        else:
            time.sleep(0.01)
    return bytes(buf)


def read_reg(ser, reg, ln):
    """Broadcast READ; returns {addr: data} from CRC-valid addr-first records (§11.2)."""
    ser.reset_input_buffer()
    ser.write(wm.make_pkt(bytes([reg >> 4, (reg & 0xF) << 4, ln]))); ser.flush()
    r = drain(ser)
    out, i, rec = {}, 0, 4 + ln + 1
    while i + rec <= len(r):
        f = r[i:i + rec]
        if wm.kf1950_crc(f[:-1]) == f[-1] and f[1] == 0 and f[3] == ln and f[0] < N:
            out[f[0]] = bytes(f[4:4 + ln]); i += rec
        else:
            i += 1
    return out


def count_frames(buf, ln):
    n, i = 0, 0
    while i + ln <= len(buf):
        if wm.kf1950_crc(buf[i:i + ln - 1]) == buf[i + ln - 1]:
            n += 1; i += ln
        else:
            i += 1
    return n


def outliers(vals, k=4.0):
    """Robust z (median/MAD). Returns {addr: value} beyond k."""
    if len(vals) < 10:
        return {}
    xs = list(vals.values()); med = statistics.median(xs)
    mad = statistics.median(abs(x - med) for x in xs) or 1
    return {a: v for a, v in vals.items() if abs(v - med) / (1.4826 * mad) > k}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freq', type=int, default=198, help='fixed PLL MHz to program (<=400 for a census)')
    ap.add_argument('--reps', type=int, default=3)
    ap.add_argument('--ticket-n', type=int, default=8)
    ap.add_argument('--json')
    ap.add_argument('--no-reset', action='store_true')
    a = ap.parse_args()
    if a.freq > 400:
        sys.exit('refusing --freq > 400: jumping straight to a high PLL kills the chain (§0.2l); ramp with the replay tool')

    ctrl_p, data_p = wm.CTRL_PORT, wm.DATA_PORT
    rep = {'started': time.strftime('%F %T'), 'freq': a.freq, 'reps': a.reps}
    if not a.no_reset:
        ctrl = serial.Serial(ctrl_p, 9600, timeout=2); time.sleep(0.1)
        wm.send_ctrl(ctrl, 0x05, 0x00, 0x06, bytes([0x00, 0x00])); time.sleep(1.0)
        wm.send_ctrl(ctrl, 0x09, 0x00, 0x06, bytes([0x00, 0x01])); ctrl.close()
        print('RESETN pulsed 1 s; waiting 15 s', flush=True); time.sleep(15)

    d = serial.Serial(data_p, wm.INIT_BAUD, timeout=1, write_timeout=10)
    # L1 enumeration -- count CRC-valid 11-B ID replies (not bytes // 11: partials lie)
    d.reset_input_buffer(); d.write(wm.make_pkt(bytes([0x10, 0x00, 0x06])))
    time.sleep(1.0); raw = drain(d, quiet=0.5, cap=4.0)
    n_enum = count_frames(raw, 11)
    rep['enum_375k'] = n_enum
    print('L1 enumeration @375000: %d CRC-valid ID replies (%d B)' % (n_enum, len(raw)), flush=True)
    if n_enum == 0:
        print('   -> ZERO. Check in order: rail held? (WM sysfs en=1, vout~1180-1350)  bitLode wedged? (USB replug)'
              '  board just died from over-frequency? (rail power-cycle)  power-up baud ~170k (try --no-reset after a rail cycle)')
        return 2
    if n_enum < N:
        print('   -> PARTIAL: chips 0x00..0x%02X reachable; suspect chip 0x%02X or the link into it'
              ' (dead chip, cracked joint, or its local rail in that string)' % (n_enum - 1, n_enum))

    d.reset_input_buffer(); d.write(wm.build_burst0()); time.sleep(0.5); drain(d)
    d.write(wm.build_burst1()); d.flush(); time.sleep(0.05)
    d.baudrate = wm.FAST_BAUD
    d.reset_input_buffer(); d.write(wm.make_pkt(bytes([0x11, 0x00, 0x01])))
    st = drain(d); n_stat = count_frames(st, 6)
    rep['status_2M'] = n_stat
    print('L2 STATUS @%d: %d chips' % (wm.FAST_BAUD, n_stat), flush=True)
    if n_stat < n_enum:
        print('   -> fewer at fast baud than enumerated: marginal link/edge on the chain, or a chain left'
              ' half-dead by a previous run (soft reset does NOT fix that -- rail power-cycle)')

    # writes whose read-back proves the per-chip write path
    d.write(rr.nonce_base_sweep()); d.flush(); time.sleep(0.2); drain(d)
    pll, fb, pd = rr.pll_freq(a.freq)
    d.write(pll); time.sleep(0.2); drain(d)
    mask = wm.ticket_mask_frame(a.ticket_n)
    d.write(mask); time.sleep(0.2); drain(d)

    expect_800 = {c: bytes([(((c << 32) // N) >> 24) & 0xFF, (((c << 32) // N) >> 16) & 0xFF]) for c in range(N)}
    tests = [('A', 0x100, 6, lambda c, v: v[:2] == b'\x19\x50'),
             ('W', 0x800, 2, lambda c, v: v == expect_800[c]),
             ('P', 0x031, 6, lambda c, v: v[:4] == bytes([fb >> 8, fb & 0xFF, 0x04, pd])),
             ('M', 0x051, 4, lambda c, v: v == mask[5:9]),
             ('T', 0x24A, 2, None), ('S', 0x236, 2, None)]
    answered = defaultdict(Counter); wrong = defaultdict(Counter); last = defaultdict(dict)
    for r in range(a.reps):
        for key, reg, ln, ok in tests:
            got = read_reg(d, reg, ln)
            for c, v in got.items():
                answered[key][c] += 1; last[key][c] = v
                if ok and not ok(c, v):
                    wrong[key][c] += 1
    d.write(wm.ticket_mask_frame(32)); time.sleep(0.05); d.close()

    print('\nper-register results (%d reps; expected %d chips each):' % (a.reps, N))
    names = {'A': 'chip ID 0x100', 'W': 'nonce base 0x800', 'P': 'PLL 0x031 @%d MHz' % a.freq,
             'M': 'ticket mask 0x051', 'T': 'die temp 0x24A', 'S': 'PVT 0x236'}
    bad = defaultdict(list)
    for key, reg, ln, ok in tests:
        full = sum(1 for c in range(N) if answered[key][c] == a.reps)
        never = [c for c in range(N) if answered[key][c] == 0]
        flaky = [c for c in range(N) if 0 < answered[key][c] < a.reps]
        wr = [c for c in range(N) if wrong[key][c]]
        print('  %s %-22s all-reps %3d | silent %s | intermittent %s | wrong value %s' % (
            key, names[key], full, fmt(never), fmt(flaky), fmt(wr)))
        for c in never: bad[c].append(key + ':silent')
        for c in flaky: bad[c].append(key + ':intermittent')
        for c in wr: bad[c].append(key + ':wrong=' + last[key][c].hex())
    for key in ('T', 'S'):
        vals = {c: int.from_bytes(v, 'big') for c, v in last[key].items()}
        if not vals:
            continue
        xs = sorted(vals.values())
        print('  %s distribution: min %d  median %d  max %d' % (key, xs[0], xs[len(xs) // 2], xs[-1]))
        for c, v in sorted(outliers(vals).items()):
            bad[c].append('%s:outlier=%d' % (key, v))
        if key == 'S':
            for c, v in vals.items():
                if v >= 1023: bad[c].append('S:saturated')
        rep[key + '_values'] = {str(c): v for c, v in vals.items()}

    print('\nverdict:')
    if not bad and n_enum == N and n_stat == N:
        print('  PASS -- all 111 chips enumerate, answer at 2 Mbaud, accept per-chip writes, hold PLL + mask,'
              ' and report plausible telemetry. (Comms/config only: run the hashing test for the cores.)')
    for c in sorted(bad):
        print('  chip 0x%02X (%3d): %s' % (c, c, ', '.join(dict.fromkeys(bad[c]))))
    rep.update(bad={str(c): v for c, v in bad.items()})
    if a.json:
        json.dump(rep, open(a.json, 'w'), indent=1); print('\njson -> ' + a.json)
    return 0 if not bad else 1


def fmt(lst):
    if not lst:
        return '-'
    s = ','.join('%02X' % c for c in lst[:12])
    return s + ('..(+%d)' % (len(lst) - 12) if len(lst) > 12 else '')


if __name__ == '__main__':
    sys.exit(main())
