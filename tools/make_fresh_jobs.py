#!/usr/bin/env python3
"""Generate FRESH work by rolling the merkle-root tail of captured WM jobs.

Why this is safe (and why it is not the §9.1 encoder trap):
the 6 midstates cover only the FIRST 64 header bytes. The merkle tail, ntime and nbits live
in the second block. Rolling the tail therefore produces genuinely new work — a nonce space
the chips have never searched — while every midstate stays valid and byte-exact as captured.
We never reconstruct a midstate from a SHA context, so the transform that trap is about is
never performed.

This is what a real miner does when it rolls extranonce.

usage: make_fresh_jobs.py [count]      -> writes jobpool/fresh_x81.bin
"""
import sys, os, struct
import kf1950_wm_init_job as wm

HERE = os.path.dirname(os.path.abspath(__file__))
N = int(sys.argv[1]) if len(sys.argv) > 1 else 240

src = open(os.path.join(HERE, 'jobpool', 'x81_jobs.bin'), 'rb').read()
base = [src[i:i + 212] for i in range(0, len(src), 212)]
out = bytearray()
seen = set()
for k in range(N):
    b = bytearray(base[k % len(base)])
    # payload offset p maps to frame offset p+3+2 (FF FF + the 0x81 opcode byte at index 2)
    # merkle tail = payload[200:204] = j[203:207] with j = frame[2:]  ->  frame[205:209]
    tail = (0xC0DE0000 + k) & 0xFFFFFFFF
    b[205:209] = struct.pack('>I', tail)
    b[-1] = wm.kf1950_crc(bytes(b[:-1]))
    assert wm.kf1950_crc(bytes(b[:-1])) == b[-1]
    assert bytes(b[205:209]) not in seen
    seen.add(bytes(b[205:209]))
    out += bytes(b)

p = os.path.join(HERE, 'jobpool', 'fresh_x81.bin')
open(p, 'wb').write(bytes(out))
print("wrote %s : %d fresh X81 jobs (212 B each), %d distinct merkle tails"
      % (p, len(out) // 212, len(seen)))
