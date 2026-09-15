#!/usr/bin/env python3
"""KF1950 LEGACY (80 04 DE) reference vector — block 125552, end to end, no dependencies.

Purpose: let a builder be validated offline, with no hardware, against a known-good
Bitcoin block. Mirrors ESP-Miner/KF1950_x81_layout_test.py for the legacy path.

  TEST 1  midstate encoding round-trips (encode -> decode recovers h0..h7).
  TEST 2  the 228-byte frame, read back through the reader mapping, reproduces
          block 125552's real hash. Proves midstate + tail + field placement
          together encode exactly the header being claimed.
  TEST 3  the builder reproduces WM Job A byte-for-byte (regression on the layout).

⚠ ONE ASSUMPTION, FLAGGED: the 32-byte midstate ENCODING (full 32-byte reversal:
mbedtls h0..h7 native-LE words -> h7..h0 big-endian words) is carried over from the
X81 path, where it is cryptographically verified. It has never been independently
confirmed for legacy, because no legacy job with a known header has ever been captured.
Everything else here is verified against the wire.
"""
import struct, hashlib

# ---------------------------------------------------------------- SHA256 core
K = [0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
     0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
     0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
     0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
     0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
     0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
     0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
     0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2]
IV = [0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]
M = 0xFFFFFFFF
def _rr(x, n): return ((x >> n) | (x << (32 - n))) & M

def compress(state, block):
    """One SHA256 compression: 8 ints + 64-byte block -> 8 ints."""
    assert len(block) == 64
    w = list(struct.unpack('>16I', block))
    for i in range(16, 64):
        s0 = _rr(w[i-15],7) ^ _rr(w[i-15],18) ^ (w[i-15] >> 3)
        s1 = _rr(w[i-2],17) ^ _rr(w[i-2],19) ^ (w[i-2] >> 10)
        w.append((w[i-16] + s0 + w[i-7] + s1) & M)
    a,b,c,d,e,f,g,h = state
    for i in range(64):
        S1 = _rr(e,6) ^ _rr(e,11) ^ _rr(e,25)
        t1 = (h + S1 + ((e & f) ^ (~e & g)) + K[i] + w[i]) & M
        S0 = _rr(a,2) ^ _rr(a,13) ^ _rr(a,22)
        t2 = (S0 + ((a & b) ^ (a & c) ^ (b & c))) & M
        h,g,f,e,d,c,b,a = g,f,e,(d+t1)&M,c,b,a,(t1+t2)&M
    return [(x+y) & M for x,y in zip(state,(a,b,c,d,e,f,g,h))]

# second header block: 16 data bytes + 0x80 + 39 zeros + 64-bit length (640 bits)
PAD = b'\x80' + b'\x00'*39 + struct.pack('>Q', 640)

# --------------------------------------------------------- midstate encoding
def encode_midstate(state):
    """h0..h7 ints -> the 32 bytes that go in a job slot (full 32-byte reversal)."""
    native = b''.join(struct.pack('<I', wd) for wd in state)
    return bytes(native[31 - i] for i in range(32))

def decode_midstate(slot_bytes):
    """The verified reader mapping: slot bytes -> h0..h7 ints."""
    return list(struct.unpack('>8I', slot_bytes))[::-1]

# ------------------------------------------------------------- legacy builder
_t = []
for _b in range(256):
    _c = _b
    for _ in range(8): _c = ((_c << 1) ^ 0x31) & 0xFF if _c & 0x80 else (_c << 1) & 0xFF
    _t.append(_c)
def crc8(d, init=0xFF):
    c = init
    for x in d: c = _t[c ^ x]
    return c

def build_legacy(sn, N, slot, midstate, nbits, ntime, merkle_tail, wire_id):
    """The 228-byte legacy job, per the model verified 3024/3024 against the WM."""
    assert sn % 0x80 == 0 and 0 <= N <= 0x7F and 0 <= slot <= 5
    assert len(midstate) == 32 and len(merkle_tail) == 4 and 0xC0 <= wire_id <= 0xFF
    V = ((sn & 0xFFFF) + N + 0x20) & 0xFFFF
    p = bytearray(b'\xFF\xFF\x80\x04\xDE')
    p += sn.to_bytes(4, 'big') + V.to_bytes(2, 'big')*3 + N.to_bytes(2, 'big') + b'\x55'*4
    for s in range(6):
        p += midstate if s == slot else b'\x55'*32
    p += nbits.to_bytes(4, 'little') + ntime.to_bytes(4, 'little') + merkle_tail
    p += bytes([wire_id, 0xAA])
    p += bytes([crc8(bytes(p))])
    assert len(p) == 228
    return bytes(p)

# ------------------------------------------------------------- block 125552
VER, NTIME, NBITS, NONCE = 1, 0x4dd7f5c7, 0x1a44b9f2, 0x9546a142
PREV = '00000000000008a3a41b85b8b29ad444def299fee21793cd8b9e567eab02cd81'
MERK = '2b12fcf1b09288fcaff797d71e950e71ae42b91e8bdb2304758dfcffc2b620e3'
HEADER = (struct.pack('<I', VER) + bytes.fromhex(PREV)[::-1] + bytes.fromhex(MERK)[::-1]
          + struct.pack('<I', NTIME) + struct.pack('<I', NBITS) + struct.pack('<I', NONCE))
REAL_HASH = hashlib.sha256(hashlib.sha256(HEADER).digest()).digest()[::-1].hex()

SN, N, SLOT, WIRE_ID = 0x6E15CA80, 0x1E, 0, 0xC1


def main():
    mid_state  = compress(IV, HEADER[0:64])          # midstate over header[0:64]
    mid_bytes  = encode_midstate(mid_state)
    tail12     = HEADER[64:76]                       # merkle[28:32] || ntime || nbits
    frame      = build_legacy(SN, N, SLOT, mid_bytes, NBITS, NTIME, HEADER[64:68], WIRE_ID)

    print("=" * 78)
    print("KF1950 LEGACY REFERENCE VECTOR — Bitcoin block 125552")
    print("=" * 78)
    print(f"header (80 B)     {HEADER.hex()}")
    print(f"real block hash   {REAL_HASH}")
    print(f"midstate h0..h7   {' '.join('%08x' % w for w in mid_state)}")
    print(f"midstate encoded  {mid_bytes.hex()}")
    print(f"tail12 (SHA order) {tail12.hex()}   = merkle[28:32] || ntime || nbits")
    print(f"nonce             {NONCE:08x}")
    print(f"\nframe params      sn=0x{SN:08X} N=0x{N:04X} slot={SLOT} id=0x{WIRE_ID:02X}"
          f"  -> expect echo mid={5-SLOT}")
    print(f"frame [213:225]   {frame[213:225].hex()}   = nbits_le || ntime_le || merkle_tail")
    print(f"\nLEGACY JOB (228 B):\n{frame.hex()}")

    ok1 = decode_midstate(mid_bytes) == mid_state
    print(f"\nTEST 1  midstate encoding round-trip .............. {'PASS' if ok1 else 'FAIL'}")

    # read the frame back the way the chip/reader does and finish the hash
    slot_back = frame[21 + 32*SLOT : 21 + 32*(SLOT+1)]
    nbits_b, ntime_b, merk_b = frame[213:217], frame[217:221], frame[221:225]
    tail_back = merk_b + ntime_b + nbits_b
    h1 = compress(decode_midstate(slot_back), tail_back + struct.pack('<I', NONCE) + PAD)
    got = hashlib.sha256(struct.pack('>8I', *h1)).digest()[::-1].hex()
    ok2 = got == REAL_HASH
    print(f"TEST 2  frame -> block 125552 hash ................ {'PASS' if ok2 else 'FAIL'}")
    print(f"        real      {REAL_HASH}")
    print(f"        recovered {got}")

    A = bytes.fromhex("ffff8004de6e15ca80cabecabecabe001e55555555c7b458bcb18be05499c5cdc"
                      "1d652fa573ebdbd02df264737a39c798b7d459cc7" + "55"*160
                      + "56b10e177d1dd35f91df7f89f4aa0a")
    rebuilt = build_legacy(0x6E15CA80, 0x1E, 0, A[21:53], 0x170EB156, 0x5FD31D7D,
                           bytes.fromhex('91df7f89'), 0xF4)
    ok3 = rebuilt == A
    print(f"TEST 3  builder reproduces WM Job A byte-exact .... {'PASS' if ok3 else 'FAIL'}")
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == '__main__':
    raise SystemExit(main())
