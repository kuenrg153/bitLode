#!/usr/bin/env python3
"""KF1950 WM-faithful init + job send script.

Uses WM address assignment format (NOT ePIC) so ALL 111 chips get addressed.
Skips final baud commands to keep ASICs at 363636 baud.
Polls at 10ms for nonce collection (fixes USB buffer overflow).
"""

import serial
import struct
import os as _os
import time
import sys
from collections import Counter

from bitlode_ports import find_ports as _find_ports
_ctrl, _data, _ = _find_ports()
CTRL_PORT = _ctrl or '/dev/ttyACM0'
DATA_PORT = _data or '/dev/ttyACM1'
# [corrected 2026-09-04] Was 363636 (= 12 MHz/33). The chips run at 375000 (= 12 MHz/32):
# measured on our WM's wire 2026-09-03, 13,358 B decoded with ZERO unframed bytes. The old
# value was a divisor off-by-one -- 3.03 % error, which accumulates to 28.8 % of a bit period
# by the stop bit (framing fails at 50 %), so it worked but consumed most of the margin and
# left almost none for the chips' own clock tolerance.
INIT_BAUD = 375000
LEGACY_INIT_BAUD = 363636   # what this tool used before 2026-09-04; kept for A/B only
FAST_BAUD = 2000000         # chain rate after burst1 (measured on hardware 2026-09-04)

# Nonce-collection settings. [added 2026-09-04]
# ⚠ The old code sent ONE job then polled 30 s. Two confounds, both fatal to a null result:
#   1. one job's 2^32 space is exhausted by 111 chips @649 MHz in ~0.06 s -- the rest of the
#      poll is dead time with nothing left to search. The WM FLOODS jobs continuously.
#   2. the WM's own first-job -> first-nonce latency is 14-33 s (no nonces at +44 s with jobs
#      since +30 s; 9,238/s by +63 s), so a 30 s window sits INSIDE the onset range measured
#      on known-good silicon.
COLLECT_S = 45.0     # total flood+collect window; must comfortably exceed the 14-33 s onset
JOB_RATE = 120.0      # jobs/s target (228 B @375 kbaud = 6.1 ms/job => ~164/s wire ceiling)
TICKET_N = 8          # nonce-report difficulty exponent: 8 = 256 = ESP-Miner default.
                      # Set 4 (=16) for the self-test-style nonce flood positive control.
NUM_CHIPS = 111


def hexstr(data):
    return ' '.join(f'{b:02X}' for b in data)


def kf1950_crc(data, init=0xFF):
    crc = init
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) if (crc & 0x80) else (crc << 1)
            crc &= 0xFF
    return crc


def make_pkt(payload, init=0xFF):
    body = bytes([0xFF, 0xFF]) + payload
    return body + bytes([kf1950_crc(body, init)])


def send_ctrl(ser, cmd_id, bus, page, cmd_bytes):
    """Send command on the bitLode control port."""
    payload = bytes([cmd_id, bus, page]) + cmd_bytes
    length = len(payload) + 2
    packet = struct.pack('<H', length) + payload
    ser.write(packet)
    time.sleep(0.3)
    hdr = ser.read(2)
    if len(hdr) < 2:
        return None
    payload_len = struct.unpack('<H', hdr)[0]
    rest = ser.read(1 + payload_len)  # cmd_id echo + payload
    return rest[1:] if len(rest) > 1 else rest


# ⚠⚠ NOT A HASH — see build_burst0(). [corrected 2026-09-04]
#
# This table was documented as "chip-silicon-specific hash bytes". It is not. Every entry is
# exactly  crc8(FF FF 00 14 02 00 [n], init 0xFF)  -- verified 111/111. It is the CRC of the
# FIRST of the two address-assignment frames, which older tooling glued into the middle of a
# single 15-byte packet and then papered over with a fake `init=0x3A` CRC seed. There is no
# 0x3A seed anywhere in this protocol (datasheet §2.4).
#
# It is retained ONLY for ADDR_ORDER='legacy' provenance and is never needed: the value is
# computed. Do not add entries; do not treat it as silicon data.
WM_HASHES = [
    0xD0, 0xE1, 0xB2, 0x83, 0x14, 0x25, 0x76, 0x47, 0x69, 0x58,  # 0-9
    0x0B, 0x3A, 0xAD, 0x9C, 0xCF, 0xFE, 0x93, 0xA2, 0xF1, 0xC0,  # 10-19
    0x57, 0x66, 0x35, 0x04, 0x2A, 0x1B, 0x48, 0x79, 0xEE, 0xDF,  # 20-29
    0x8C, 0xBD, 0x56, 0x67, 0x34, 0x05, 0x92, 0xA3, 0xF0, 0xC1,  # 30-39
    0xEF, 0xDE, 0x8D, 0xBC, 0x2B, 0x1A, 0x49, 0x78, 0x15, 0x24,  # 40-49
    0x77, 0x46, 0xD1, 0xE0, 0xB3, 0x82, 0xAC, 0x9D, 0xCE, 0xFF,  # 50-59
    0x68, 0x59, 0x0A, 0x3B, 0xED, 0xDC, 0x8F, 0xBE, 0x29, 0x18,  # 60-69
    0x4B, 0x7A, 0x54, 0x65, 0x36, 0x07, 0x90, 0xA1, 0xF2, 0xC3,  # 70-79
    0xAE, 0x9F, 0xCC, 0xFD, 0x6A, 0x5B, 0x08, 0x39, 0x17, 0x26,  # 80-89
    0x75, 0x44, 0xD3, 0xE2, 0xB1, 0x80, 0x6B, 0x5A, 0x09, 0x38,  # 90-99
    0xAF, 0x9E, 0xCD, 0xFC, 0xD2, 0xE3, 0xB0, 0x81, 0x16, 0x27,  # 100-109
    0x74,                                                           # 110
]


# WM core-enable chip address ranges (from WM CSV ground truth)
# Chips 0x01-0x0F, 0x21-0x30, 0x41-0x50, 0x61-0x70 = 63 chips
WM_CE_CHIPS = (list(range(0x01, 0x10)) + list(range(0x21, 0x31)) +
               list(range(0x41, 0x51)) + list(range(0x61, 0x71)))


# Address-assignment data-byte order. BOTH orders are real, CRC-clean captures:
#   'wire'   FF FF 00 14 02 [n] 00 [crc] + [n] 00 02 04 01 [b12] [crc]
#            -- what OUR WM puts on the wire, measured 2026-09-03, 222/222 frames.
#              Matches the documented addressed-frame shape `[addr] 00 [b2] [b3] ...`.
#   'legacy' FF FF 00 14 02 00 [n] [crc] + 00 [n] 02 04 01 [b12] [crc]
#            -- what vendor/KF1950_Comms-main/Initialization/WM_Initialization code.csv holds,
#              111/111 CRC-clean at init 0xFF. Different firmware, NOT a parsing artifact.
#              Every addressed frame here targets chip 0x00 and puts [n] in the `00` slot.
# Default is 'wire': this rig's purpose is reproducing OUR WM.
#
# ─── PROFILE ───────────────────────────────────────────────────────────────────
# The legacy vendor CSV and our own WM run DIFFERENT FIRMWARE, and they differ in
# four places, not one. Selecting a profile sets all four together; mixing them
# reproduces neither controller. [added 2026-09-04]
#
#   'wm2026'  reproduce OUR WM as measured 2026-09-03
#             (sessions/coldstart_steady_20260902/, 111/111 address frames byte-exact)
#   'legacy'  reproduce vendor/KF1950_Comms-main/Initialization/WM_Initialization code.csv
#             -- byte-identical to this tool's pre-2026-09-04 output (5726 / 1341 B)
#
# | difference              | 'legacy'                  | 'wm2026'                  |
# |-------------------------|---------------------------|---------------------------|
# | address data bytes      | 00 [n], addressed to 0x00 | [n] 00, addressed to n    |
# | NONCE_RANGE last byte   | 0x30                      | 0x00                      |
# | reg 0x220/0x014/0x010   | absent                    | present                   |
# | MISC_PLL 04 00,         | present                   | absent                    |
# |   reg 0x200, reg 0x201  |                           |                           |
#
# ⚠ The last row is the weakest evidence in this table: those three frames are absent
# from BOTH captured windows (slow and fast), but those windows are not proven to span
# the WM's entire bring-up. "Not observed" is weaker than "never sent". If a future
# capture finds them, move them back into 'wm2026'.
PROFILE = 'wm2026'


def _p(profile=None):
    pr = profile or PROFILE
    if pr not in ('wm2026', 'legacy'):
        raise ValueError(f"PROFILE must be 'wm2026' or 'legacy', got {pr!r}")
    return pr


ADDR_ORDER = 'wire'   # overridden by PROFILE in build_addr_assign(); kept for direct callers


def build_addr_assign(chip, order=None):
    """The address-assignment pair for one chip: TWO frames, both CRC init 0xFF.

    Returns (broadcast_frame, addressed_frame).
    """
    order = order or ('wire' if _p() == 'wm2026' else 'legacy')
    b12 = wm_addr_b12(chip)
    if order == 'wire':
        f1 = make_pkt(bytes([0x00, 0x14, 0x02, chip, 0x00]))     # reg 0x001 = chip
        f2 = bytes([chip, 0x00, 0x02, 0x04, 0x01, b12])          # addressed: chip, reg 0x020
    elif order == 'legacy':
        f1 = make_pkt(bytes([0x00, 0x14, 0x02, 0x00, chip]))
        f2 = bytes([0x00, chip, 0x02, 0x04, 0x01, b12])
    else:
        raise ValueError(f"ADDR_ORDER must be 'wire' or 'legacy', got {order!r}")
    return f1, f2 + bytes([kf1950_crc(f2)])


def ticket_mask_frame(n):
    """reg 0x051 write for difficulty 2^n.

    Encoding per datasheet §0.2h: value is 2^n - 1, written MSB-first with EVERY BYTE
    BIT-REVERSED. Cross-checked against the three values seen on our own wire:
      n=8  -> 00 00 00 ff (256, ESP-Miner default)   n=13 -> 00 00 f8 ff (8192)
      n=2  -> 00 00 00 c0 (4)                        n=32 -> ff ff ff ff (park)
    """
    if not 0 <= n <= 32:
        raise ValueError(f"ticket exponent out of range: {n}")
    mask = (1 << n) - 1
    rev = lambda b: int(f"{b:08b}"[::-1], 2)
    body = [rev((mask >> sh) & 0xFF) for sh in (24, 16, 8, 0)]
    return make_pkt(bytes([0x05, 0x14, 0x04]) + bytes(body))


TICKET_PARK = None   # filled at import: reg 0x051 = 0xFFFFFFFF, "report nothing"


def wm_addr_b12(chip):
    """WM b12: 0x31 only when chip % 12 == 11, else 0x30."""
    return 0x31 if chip % 12 == 11 else 0x30


def build_job_228(job_id, b6, b7, slot, midstate, ntime_4b, nbits_3b, job_data_4b, counter_byte):
    """Build correct 228-byte KF1950 job packet."""
    pkt = bytearray()
    pkt += bytes([0xFF, 0xFF, 0x80, 0x04, 0xDE])
    pkt += bytes([job_id, b6, b7])
    pkt += bytes([0x80 if slot in [1, 2] else 0x00])
    pkt += bytes([0x00])
    slot_markers = {0: 0x58, 1: 0x18, 2: 0x45, 3: 0x43, 4: 0x3B}
    byte_10 = slot_markers[slot]
    pkt += bytes([byte_10])
    xor_masks = {0: 0x20, 1: 0xA0, 2: 0xA0, 3: 0x20, 4: 0x60}
    xor_val = byte_10 ^ xor_masks[slot]
    pkt += bytes([b7, xor_val, b7, xor_val, b7, xor_val])
    pkt += bytes([0x55] * 4)
    for i in range(4):
        pkt += midstate if i == slot else bytes([0x55] * 32)
    # 64 bytes extended padding (NOT 65!)
    if slot == 4:
        pkt += midstate + bytes([0x55] * 32)
    else:
        pkt += bytes([0x55] * 64)
    pkt += bytes([0x56])
    pkt += ntime_4b
    pkt += nbits_3b
    pkt += job_data_4b
    pkt += bytes([counter_byte])
    pkt += bytes([0xAA])
    crc = kf1950_crc(pkt, init=0xFF)
    pkt += bytes([crc])
    assert len(pkt) == 228, f"Packet is {len(pkt)} bytes, expected 228"
    return bytes(pkt)


def show_status(label, raw):
    """Print all 6 bytes of first N chip status frames."""
    n = min(5, len(raw) // 6)
    frames = len(raw) // 6
    print(f"  Status ({label}): {len(raw)}B ({frames} chips)")
    for i in range(n):
        f = raw[i*6:(i+1)*6]
        crc_ok = kf1950_crc(f[:5]) == f[5]
        print(f"    chip[{i}]: {' '.join(f'{b:02X}' for b in f)}  b2=0x{f[2]:02X} b3=0x{f[3]:02X} b4=0x{f[4]:02X}  crc={'OK' if crc_ok else 'FAIL'}")


def build_burst0():
    """Build burst0 bytes (matches WM CSV bytes 0-5725 exactly)."""
    out = bytearray()
    # Config
    out += make_pkt(bytes([0x12, 0x34, 0x02, 0x13, 0xF4]))
    out += make_pkt(bytes([0x07, 0x04, 0x02, 0x15, 0x15]))
    out += make_pkt(bytes([0x00, 0x04, 0x01, 0xAA]))
    # Address assignment: TWO frames per chip, both CRC init 0xFF. [fixed 2026-09-04]
    # Was one glued 15-byte packet with a fake init=0x3A seed; see build_addr_assign().
    for chip in range(NUM_CHIPS):
        f1, f2 = build_addr_assign(chip)
        out += f1 + f2
    # Baud div fast
    out += make_pkt(bytes([0x07, 0x04, 0x02, 0x35, 0x35]))
    # PLL: 4 domains × (SEL_05, DATA, SEL_04, SEL_04dup, DATA)
    for domain in range(4):
        out += make_pkt(bytes([0x03, 0x04, 0x01, 0x05]))
        out += make_pkt(bytes([0x03, 0x14, 0x06, 0x00, 0x80, 0x04, 0x05, domain, 0xAA]))
        out += make_pkt(bytes([0x03, 0x04, 0x01, 0x04]))
        out += make_pkt(bytes([0x03, 0x04, 0x01, 0x04]))
        out += make_pkt(bytes([0x03, 0x14, 0x06, 0x00, 0x80, 0x04, 0x05, domain, 0xAA]))
    # Nonce range + hash mode
    # NONCE_RANGE -- final byte is 0x30 in the legacy CSV, 0x00 on our WM. This sits inside
    # the nonce-space partition, so it is NOT cosmetic; neither value is decoded.
    out += make_pkt(bytes([0x80, 0x44, 0x0C,
                            0x00, 0x00, 0x00, 0x20, 0x00, 0x40, 0x00, 0x60,
                            0x00, 0x00, 0x00,
                            0x00 if _p() == 'wm2026' else 0x30]))
    out += make_pkt(bytes([0x8D, 0xD4, 0x01, 0xAA]))
    # Core enable: mask=0x31, 63 chips × 6 reps consecutive
    for chip in WM_CE_CHIPS:
        for _ in range(6):
            out += make_pkt(bytes([0x04, 0x04, 0x04, chip, 0x3F, 0x31, 0xAA]))
    # Post config
    out += make_pkt(bytes([0x02, 0x14, 0x01, 0x3A]))                       # reg 0x021
    if _p() == 'wm2026':
        # reg 0x220 -- absent from the legacy CSV. Present in the WM bring-up between
        # reg 0x021 and reg 0x230; one of the 15 cold-start-only frames (datasheet §0.2i).
        # ABLATION SWITCH (KF_NO_220=1). reg 0x220 is one of the ~12 writes we can locate but
        # not explain (§0.2d) -- AND it is the frame nini's bring-up omitted, on a board that
        # ingests jobs but never emits `04 06`. Omitting it here tests whether it is the
        # recognise gate. Read-back 2026-09-11 proves `13 1E` IS stored at 0x220-0x221 on all
        # 111 chips, so it is real persistent state, not a strobe.
        if _os.environ.get('KF_NO_220', '') != '1':
            out += make_pkt(bytes([0x22, 0x04, 0x03, 0x13, 0x1E, 0xAA]))   # reg 0x220
    out += make_pkt(bytes([0x23, 0x04, 0x06, 0x26, 0x1F, 0x30, 0x80, 0x00, 0xAA]))
    out += make_pkt(bytes([0x24, 0x04, 0x07, 0x25, 0xFE, 0x50, 0x00, 0x60, 0x00, 0x00]))
    out += make_pkt(bytes([0x24, 0x94, 0x01, 0xAA]))
    # Status read (last packet of burst0 — generates 111×6=666B responses)
    out += make_pkt(bytes([0x11, 0x00, 0x01]))
    return bytes(out)


def build_burst1():
    """Build burst1 bytes (matches WM CSV bytes 5726:-17 exactly)."""
    out = bytearray()
    # reg 0x051 = 0xFFFFFFFF -- the TICKET-MASK PARK (datasheet §11.7), not a "trigger".
    # [clarified 2026-09-04] This used to be written as two raw uncrc'd literals. It is ONE
    # ordinary frame and the trailing E3 is its CRC. The bytes on the wire are IDENTICAL
    # either way -- this change is correctness of understanding, not of output.
    out += make_pkt(bytes([0x05, 0x14, 0x04, 0xFF, 0xFF, 0xFF, 0xFF]))
    out += make_pkt(bytes([0x05, 0x04, 0x01, 0x83]))                       # reg 0x050
    out += make_pkt(bytes([0x06, 0x04, 0x01, 0x11]))                       # reg 0x060
    # pll_phase2
    out += make_pkt(bytes([0x03, 0x04, 0x01, 0x04]))
    out += make_pkt(bytes([0x03, 0x14, 0x06, 0x00, 0x80, 0x04, 0x02, 0x03, 0xAA]))
    # B1 second pass: 2 full passes × 63 chips
    for _ in range(2):
        for chip in WM_CE_CHIPS:
            out += make_pkt(bytes([0x04, 0x04, 0x04, chip, 0x3F, 0xB1, 0xAA]))
    out += make_pkt(bytes([0x03, 0x04, 0x01, 0x04]))
    if _p() == 'legacy':
        # Phase 9 "final PLL commit" -- legacy CSV only. ⚠ Not observed in EITHER 2026-09-03
        # window (slow or fast); see the PROFILE table for why that is weak evidence.
        out += make_pkt(bytes([0x03, 0x14, 0x06, 0x00, 0x80, 0x04, 0x04, 0x00, 0xAA]))
        out += make_pkt(bytes([0x20, 0x04, 0x01, 0x04]))                   # reg 0x200
        out += make_pkt(bytes([0x20, 0x14, 0x06, 0x00, 0x80, 0x04, 0x04, 0x0F, 0xAA]))
    else:
        # The WM's own tail: pll_phase2 pair, then reg 0x014 and reg 0x010 -- both
        # cold-start-only frames (datasheet §0.2i) at the very end of the 375 kbaud phase.
        out += make_pkt(bytes([0x03, 0x14, 0x06, 0x00, 0x80, 0x04, 0x02, 0x03, 0xAA]))
        out += make_pkt(bytes([0x01, 0x44, 0x02, 0x02, 0x00]))             # reg 0x014
        out += make_pkt(bytes([0x01, 0x04, 0x04, 0x01, 0x80, 0x09, 0xAA])) # reg 0x010
    return bytes(out)


def main():
    print("=== KF1950 WM-Faithful Init + Job ===\n")

    # Pre-generate init bursts and verify sizes
    burst0 = build_burst0()
    burst1 = build_burst1()
    print(f"Init bursts: burst0={len(burst0)}B burst1={len(burst1)}B")
    # [updated 2026-09-04] Sizes are profile-dependent. 'legacy' reproduces this tool's
    # pre-2026-09-04 output byte-for-byte (5726/1341) -- that equality is the regression test.
    EXPECT = {'wm2026': (5735, 1340), 'legacy': (5726, 1341)}
    e0, e1 = EXPECT[_p()]
    assert len(burst0) == e0, f"burst0 size wrong for {PROFILE}: {len(burst0)} != {e0}"
    assert len(burst1) == e1, f"burst1 size wrong for {PROFILE}: {len(burst1)} != {e1}"
    print(f"  profile={PROFILE}")

    # Phase 0: Reset
    print("\nPhase 0: Reset ASIC chain")
    # Keep ctrl open throughout — closing it before data port opens causes firmware to get stuck
    ctrl = serial.Serial(CTRL_PORT, 9600, timeout=2)
    time.sleep(0.1)
    send_ctrl(ctrl, 0x05, 0x00, 0x06, bytes([0x00, 0x00]))  # RESETN LOW
    time.sleep(1.0)
    send_ctrl(ctrl, 0x09, 0x00, 0x06, bytes([0x00, 0x01]))  # RESETN HIGH
    print("  Reset done, waiting 15s...")
    time.sleep(15.0)

    # Phase 1: Chip ID
    print("\nPhase 1: Chip ID")
    data = serial.Serial(DATA_PORT, INIT_BAUD, timeout=3)
    data.reset_input_buffer()
    data.write(make_pkt(bytes([0x10, 0x00, 0x06])))
    time.sleep(3.0)
    avail = data.in_waiting
    if avail:
        chip_id_raw = data.read(min(avail, 4096))
        n_chips = avail // 11
        print(f"  Got {n_chips} chips ({avail} bytes)")
        # Show first chip's 11-byte response: FF FF 00 06 [chip_id_hi] [chip_id_lo] [b6..b9] [CRC]
        if len(chip_id_raw) >= 11:
            f0 = chip_id_raw[:11]
            chip_id_val = (f0[4] << 8) | f0[5]
            print(f"  Chip ID response[0]: {f0.hex(' ')}  chip_type=0x{chip_id_val:04X}")
            print(f"    bytes[6..9]={f0[6:10].hex(' ')} (status/version fields)")
    else:
        # [added 2026-09-04] Don't fail blind: a previous run can leave the chain at the
        # fast rate, and a short power-off may not clear it. Probe before giving up.
        print("  No chips at %d -- probing other rates..." % INIT_BAUD)
        found = False
        for probe in (FAST_BAUD, LEGACY_INIT_BAUD, 1000000):
            data.baudrate = probe
            data.reset_input_buffer()
            data.write(make_pkt(bytes([0x10, 0x00, 0x06])))
            time.sleep(1.5)
            n = data.in_waiting
            print(f"    probe @{probe}: {n}B")
            if n:
                r = data.read(min(n, 4096))
                print(f"      {r[:22].hex(' ')}")
                found = True
                break
        data.baudrate = INIT_BAUD
        if not found:
            print("  FATAL: No chips at any rate -- chain is unpowered or held in reset.")
        data.close()
        return

    # Pre-init status
    data.reset_input_buffer()
    data.write(make_pkt(bytes([0x11, 0x00, 0x01])))
    time.sleep(1.0)
    avail = data.in_waiting
    if avail:
        show_status("pre-init", data.read(min(avail, 2048)))
    else:
        print("  Status (pre-init): no response")

    # Phase 2-5: Send burst0 as a single atomic write (no inter-packet delays)
    # Verified byte-for-byte identical to WM CSV burst0 via compare_init.py
    print(f"\nBurst0: {len(burst0)}B (config + 111 addr-assign + PLL + CE + post-config + STATUS_READ)")
    data.reset_input_buffer()
    data.write(burst0)
    # Wait for burst0 to finish transmitting + all 111 chips to respond to STATUS_READ
    # 5726 bytes @ 363636 baud ≈ 157ms TX, then 111×6=666B RX ≈ 18ms
    time.sleep(0.5)
    avail = data.in_waiting
    if avail:
        raw = data.read(min(avail, 4096))
        # Last thing in burst0 is STATUS_READ — responses are 6B each
        show_status("post-burst0 (STATUS_READ)", raw)
        # Check for spontaneous auto-report (b3=0x80 after CE = PLL locked)
        for i in range(len(raw) // 6):
            if raw[i*6+3] == 0x80:
                print(f"  *** b3=0x80 at chip[{i}] — PLL LOCKED after burst0! ***")
    else:
        print("  No response after burst0")
    data.reset_input_buffer()

    # 21ms inter-burst gap (WM timing)
    time.sleep(0.021)

    # Phase 6-9: Send burst1 as a single atomic write
    # Contains: trigger, pll_phase2, B1×2, Phase9 Final PLL
    print(f"\nBurst1: {len(burst1)}B (ticket park + reg 0x050/0x060 + pll_phase2 + tail)")
    data.write(burst1)
    data.flush()
    # ⚠⚠ [FIXED 2026-09-04] THE CHAIN CHANGES BAUD HERE AND THIS TOOL NEVER FOLLOWED IT.
    # burst0 carries the divider write (07 04 02 35 35), but the rate only takes effect once
    # burst1's PLL work lands -- exactly as the WM does it (slow phase ends with reg 0x014 /
    # reg 0x010, then the switch). Proven on hardware 2026-09-04: after burst1 a STATUS_READ
    # at 375000 returns NOTHING, while the same read at 2000000 returns 111/111 CRC-OK frames.
    # Everything this tool sent after burst1 -- the ticket-mask writes and every job -- was
    # therefore transmitted at a rate the chain was no longer listening on.
    time.sleep(0.05)
    data.baudrate = FAST_BAUD
    print(f"  >>> switched host UART to {FAST_BAUD} baud (chain switched during burst1)")
    # Wait for burst1 TX to complete (~37ms) + PLL settle time
    time.sleep(0.1)
    avail = data.in_waiting
    if avail:
        raw = data.read(min(avail, 4096))
        print(f"  Burst1 RX: {avail}B ({avail//6} frames)")
    else:
        print("  No burst1 RX")
    data.reset_input_buffer()

    # STATUS_READ after burst1 — this is the real PLL lock check.
    # burst0's STATUS_READ is sent BEFORE pll_phase2+final PLL (which are in burst1),
    # so b3=0x01 there is normal. This check is what matters.
    print("\nPost-burst1 STATUS_READ (PLL lock check):")
    data.write(make_pkt(bytes([0x11, 0x00, 0x01])))
    time.sleep(0.5)  # 111 chips × 6B × 27.5µs = 18ms TX + margin
    avail = data.in_waiting
    if avail:
        raw = data.read(min(avail, 4096))
        show_status("post-burst1", raw)
        # ⚠ [corrected 2026-09-04] The old "b3=0x80 means PLL locked" check was based on a
        # reading the datasheet RETRACTED (§11.3): b3 is the RESPONSE LENGTH, not a status
        # flag. b3=0x01 here means "1 data byte follows" and says nothing about the PLL.
        # It printed "PLL NOT LOCKED" on every healthy run. Report the field, claim nothing.
        b3s = Counter(raw[i*6+3] for i in range(len(raw)//6))
        print(f"  b3 (response LENGTH, not a status flag - §11.3): {dict(b3s)}")
    else:
        print("  No STATUS_READ response after burst1!")
        # [added 2026-09-04] Is the chain silent, or did it move baud? burst0 contains the
        # 07 04 02 35 35 divider write, so a rate change here is a live possibility.
        for probe in (LEGACY_INIT_BAUD, 2000000, 1000000):
            try:
                data.baudrate = probe
                data.reset_input_buffer()
                data.write(make_pkt(bytes([0x11, 0x00, 0x01])))
                time.sleep(0.4)
                n = data.in_waiting
                print(f"    probe @{probe}: {n}B")
                if n:
                    r = data.read(min(n, 4096))
                    print(f"      {r[:24].hex(' ')}")
                    ok = sum(1 for i in range(len(r)//6)
                             if kf1950_crc(r[i*6:i*6+5]) == r[i*6+5])
                    print(f"      {ok}/{len(r)//6} frames CRC-OK at {probe}")
                    if ok:
                        print(f"    *** CHAIN IS AT {probe} BAUD, not {INIT_BAUD} ***")
                        break
            except Exception as e:
                print(f"    probe @{probe} failed: {e}")
        data.baudrate = INIT_BAUD
    data.reset_input_buffer()

    print("  Sending job 21ms after STATUS_READ")

    # Phase 6: Send job + capture
    print("\nPhase 6: Send job + capture nonces")
    time.sleep(0.021)  # 21ms gap matching WM timing

    cap_midstate = bytes.fromhex('76B0FF424929DCD936A04181EBF1CF0D5EA6E709FE9B84007E040EFF63E8458E')
    cap_ntime = bytes([0xB1, 0x0E, 0x17, 0x59])
    cap_nbits = bytes([0x15, 0xD3, 0x5F])

    # ── Unpark the nonce-report threshold ────────────────────────────────────────
    # burst1 leaves reg 0x051 at 0xFFFFFFFF (difficulty 2^32 = report nothing). The WM
    # parks there and MOVES OFF IT to hash (datasheet §11.7). Without this write the chain
    # cannot report a nonce no matter how much work it is given.
    print(f"\n  Unparking ticket mask: difficulty 2^{TICKET_N} = {1 << TICKET_N}")
    data.write(ticket_mask_frame(TICKET_N))
    time.sleep(0.05)
    data.reset_input_buffer()

    # ── Job flood ────────────────────────────────────────────────────────────────
    # Each job must be DISTINCT work: one job's 2^32 space is exhausted by 111 chips at
    # 649 MHz in ~0.06 s, so a repeated job searches nothing new. Vary job_id, midstate
    # slot and the counter byte, exactly as the WM rotates them.
    period = 1.0 / JOB_RATE
    print(f"  Flooding jobs for {COLLECT_S:.0f}s at ~{JOB_RATE:.0f}/s "
          f"(onset on known-good silicon is 14-33 s -- do NOT shorten this)")
    all_rx = bytearray()
    t_start = time.time()
    n_jobs = 0
    next_job = t_start
    last_report = t_start
    first_rx_at = None
    while True:
        now = time.time()
        elapsed = now - t_start
        if elapsed >= COLLECT_S:
            break
        if now >= next_job:
            jid = n_jobs & 0xFF
            job = build_job_228(0x80, 0x76, 0x42, n_jobs % 5, cap_midstate,
                                cap_ntime, cap_nbits,
                                bytes([0x52, 0x88, 0x36, jid]), (n_jobs >> 8) & 0xFF)
            data.write(job)
            n_jobs += 1
            next_job += period
            if next_job < now:          # fell behind: resync rather than spiral
                next_job = now + period
        avail = data.in_waiting
        if avail:
            all_rx += data.read(avail)
            if first_rx_at is None:
                first_rx_at = elapsed
                print(f"    first RX byte at t+{elapsed:.1f}s")
        else:
            time.sleep(0.002)
        if now - last_report >= 15.0:
            last_report = now
            print(f"    t+{elapsed:5.1f}s  jobs={n_jobs:6d}  rx={len(all_rx):8d}B")

    print(f"  Sent {n_jobs} jobs in {COLLECT_S:.0f}s "
          f"({n_jobs / COLLECT_S:.1f}/s), collected {len(all_rx)} bytes")
    if first_rx_at is None:
        print("  *** ZERO bytes received across the whole window ***")

    # ── Post-flood readback ──────────────────────────────────────────────────────
    # Did our writes actually land? Read back the ticket mask we set, plus chip ID and the
    # core-enable register, all at the rate the chain is now on.
    def rd(reg, nbytes, label):
        b2 = (reg >> 4) & 0xFF
        b3 = ((reg & 0xF) << 4) | 0x0        # access nibble 0 = READ
        data.reset_input_buffer()
        data.write(make_pkt(bytes([b2, b3, nbytes])))
        time.sleep(0.4)
        n = data.in_waiting
        r = data.read(min(n, 4096)) if n else b''
        print(f"    reg 0x{reg:03X} ({label}): {n}B  {r[:14].hex(' ')}")
        return r

    print("\n  Post-flood readback @%d baud:" % data.baudrate)
    rd(0x100, 0x06, "chip ID")
    rd(0x051, 0x04, "ticket mask -- should be our unpark, not ff ff ff ff")
    rd(0x040, 0x04, "core enable")
    rd(0x24A, 0x02, "die temp")

    # Re-park on the way out, so the chain is left as we found it.
    data.write(ticket_mask_frame(32))
    time.sleep(0.02)
    raw_path = '/tmp/kf1950_nonce_raw.bin'
    with open(raw_path, 'wb') as f:
        f.write(bytes(all_rx))
    print(f"  Saved to {raw_path}")
    data.close()
    ctrl.close()

    if not all_rx:
        print("\n  No nonce data!")
        return

    # Analysis
    raw = bytes(all_rx)
    total = len(raw)
    frames = total // 11

    valid_chips = Counter()
    valid_nonces = Counter()
    for i in range(frames):
        frame = raw[i*11:(i+1)*11]
        if kf1950_crc(frame[:10]) == frame[10]:
            valid_chips[frame[1]] += 1
            valid_nonces[struct.unpack('<I', frame[4:8])[0]] += 1

    print(f"\n  {frames} frames, {sum(valid_chips.values())} CRC-OK")
    print(f"  {len(valid_chips)} unique chips responding:")
    for chip in sorted(valid_chips.keys())[:20]:
        print(f"    Chip 0x{chip:02X}: {valid_chips[chip]} nonces")
    if len(valid_chips) > 20:
        print(f"    ... and {len(valid_chips)-20} more")
    print(f"  {len(valid_nonces)} unique nonce values")

    print("\n=== Done ===")


if __name__ == '__main__':
    main()
