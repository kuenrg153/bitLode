#!/usr/bin/env python3
"""SHA256d-verify KF1950 nonces from an RX capture, with a MANDATORY known-good control.

usage: verify_nonces.py <rx.bin> [--jobs POOL] [--shuffle] [--per-chip [OUT.json]]

⚠ The control is not optional and not skippable. On 2026-09-04 a verifier with every offset
off by two returned "max 2 leading-zero bits" on real nonces and was one step from being
reported as proof the silicon was not hashing. It was caught only by running the stock
controller's known-good nonces through the same code. A verifier that has not been run
against known-good data is not evidence.

Mapping (from tools/verify_testA.py, validated on 30,000+ real nonces):
    job payload indexed FROM THE 0x81 OPCODE BYTE  (strip the FF FF preamble -> j = frame[2:])
    slot     = 5 - mid_field
    midstate = j[3+32*slot : 35+32*slot], 8 BE words in REVERSED order (h7..h0)
    tail     = j[203:207] || j[199:203] || j[195:199]     merkle || ntime || nbits
    h1 = compress(midstate, tail || nonce || PAD) ; hash = sha256(h1)
"""
import sys, os, struct, hashlib, collections, importlib.util, random

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("lrv", os.path.join(HERE, "legacy_reference_vector.py"))
lrv = importlib.util.module_from_spec(spec); sys.modules["lrv"] = lrv
try: spec.loader.exec_module(lrv)
except SystemExit: pass
compress = lrv.compress
PAD = b'\x80' + b'\x00' * 39 + struct.pack('>Q', 640)
THRESH = 24


def crc8(d, init=0xFF):
    x = init
    for b in d:
        x ^= b
        for _ in range(8):
            x = ((x << 1) ^ 0x31) & 0xFF if x & 0x80 else (x << 1) & 0xFF
    return x


def lz(x):
    v = int.from_bytes(x[::-1], 'big')
    return 256 - v.bit_length() if v else 256


def load_jobs(path=None):
    p = path or os.path.join(HERE, 'jobpool', 'x81_jobs.bin')
    if not os.path.exists(p):
        raise SystemExit("job pool not found: %s" % p)
    b = open(p, 'rb').read()
    return [b[i:i + 212][2:] for i in range(0, len(b), 212)]   # strip FF FF


def scan(rx):
    out, i = [], 0
    while i < len(rx) - 10:
        if rx[i + 2:i + 4] == b'\x04\x06' and crc8(rx[i:i + 10]) == rx[i + 10]:
            out.append(rx[i:i + 11]); i += 11
        else:
            i += 1
    return out


def verify(jobs, frames, shuffle=False):
    byid = {}
    for j in jobs: byid.setdefault(j[207], []).append(j)
    if shuffle:
        # [2026-09-14] must be a DERANGEMENT. A plain random.shuffle leaves ~1 of the job ids
        # holding its OWN list, so its nonces verify genuinely (34-37 bits): rx_sync2 gave
        # 22/1085 and 9/1085 "shuffled" hits, clustered on single job ids.
        keys = list(byid.keys()); vals = [byid[k] for k in keys]
        rot = random.randrange(1, len(keys)) if len(keys) > 1 else 0
        order = list(range(len(keys))); random.shuffle(order)
        byid = {keys[order[i]]: vals[order[(i + rot) % len(keys)]] for i in range(len(keys))}
        assert all(byid[k] is not v for k, v in zip(keys, vals)), "shuffle left a job id on its own list" 
    ev = {}
    for f in frames:
        if f[9] <= 0xBF: ev.setdefault((f[4:8], f[8], f[9]), set()).add(f[0])
    ver = []
    for (val, mid, jid), chips in ev.items():
        slot = 5 - mid if 0 <= mid <= 5 else None
        done = False
        for j in (byid.get(jid) or jobs):
            tail = j[203:207] + j[199:203] + j[195:199]
            for sl in ([slot] if slot is not None else range(6)):
                m = j[3 + 32 * sl:35 + 32 * sl]
                if len(m) < 32: continue
                st = list(struct.unpack('>8I', m))[::-1]
                z = lz(hashlib.sha256(struct.pack('>8I', *compress(st, tail + val + PAD))).digest())
                if z >= THRESH:
                    ver.append((z, val.hex(), mid, jid, len(chips))); done = True; break
            if done: break
    return ev, ver


def control():
    """Known-good: two real nonces from the stock controller. MUST verify at ~40 bits."""
    import pickle, glob
    pk = [os.environ['KF_CONTROL_PKL']] if os.environ.get('KF_CONTROL_PKL') else glob.glob('**/x81b.pkl', recursive=True)
    if not pk:
        print("  ⚠ CONTROL DATA MISSING -- results below are UNVALIDATED"); return False
    d = pickle.load(open(pk[0], 'rb'))
    jobs = [bytes(f[5])[2:] for f in d['frames']
            if f[1] == 'TX' and bytes(f[5])[:5] == b'\xff\xff\x81\x04\xce']
    rx = [bytes(f[5]) for f in d['frames']
          if f[1] == 'RX' and len(f[5]) == 11 and bytes(f[5])[2:4] == b'\x04\x06']
    ev, ver = verify(jobs, rx)
    ok = len(ver) == len(ev) and len(ev) > 0 and min(v[0] for v in ver) >= 32
    print("  CONTROL (stock controller): %d/%d verified, bits %s  -> %s"
          % (len(ver), len(ev), sorted(v[0] for v in ver), "PASS" if ok else "*** FAIL ***"))
    return ok


STEP = 2**32 / 111.0


def per_chip(ev, ver, path):
    """[2026-09-14] Per-chip attribution of VERIFIED nonces, two independent ways:
      owner    = nonce slice, floor(nonce_BE / (2^32/111)) -- valid only when the run sent the
                 §7.2b nonce-base sweep (base = floor(addr*2^32/111)); method of
                 sessions/bitaxe_crossref_20260902/nonce_owner.py
      reporter = chip address byte of the report frame, counted ONLY for single-reporter events
                 (independent of the nonce value). ⚠ Not min(reporters): relayed and chain-wide
                 reports all collapse onto chip 0 that way (chip0=632, chip110=0 on rx_pt4).
    Their agreement rate is printed so neither is trusted blind."""
    import json
    byval = {}
    for (val, mid, jid), chips in ev.items():
        byval[(val.hex(), mid, jid)] = chips
    own, rep, agree, nrep = collections.Counter(), collections.Counter(), 0, collections.Counter()
    for z, vhex, mid, jid, _n in ver:
        o = min(110, int(int(vhex, 16) // STEP))
        chips = byval[(vhex, mid, jid)]
        own[o] += 1; agree += (o in chips); nrep[min(len(chips), 3)] += 1
        if len(chips) == 1: rep[next(iter(chips))] += 1
    n = len(ver)
    print("  per-chip: slice owner among reporters for %d/%d verified events (%.1f%%)"
          % (agree, n, 100 * agree / n))
    print("  per-chip: reporters per verified event: 1=%d 2=%d 3+=%d" % (nrep[1], nrep[2], nrep[3]))
    rmid = sorted(rep.get(c, 0) for c in range(1, 110))
    print("  per-chip (single reporter): chip0=%d chip110=%d | chips1-109 min %d median %d max %d"
          % (rep.get(0, 0), rep.get(110, 0), rmid[0], rmid[len(rmid) // 2], rmid[-1]))
    mid_counts = sorted(own.get(c, 0) for c in range(1, 110))
    q = lambda f: mid_counts[int(f * (len(mid_counts) - 1))]
    print("  per-chip (owner): chip0=%d chip110=%d | chips1-109 min %d q25 %d median %d q75 %d max %d"
          % (own.get(0, 0), own.get(110, 0), mid_counts[0], q(.25), q(.5), q(.75), mid_counts[-1]))
    i = sys.argv.index('--per-chip')
    out = sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith('--') else None
    if out:
        json.dump({'file': os.path.basename(path), 'verified': n, 'agree': agree,
                   'owner': {str(c): own.get(c, 0) for c in range(111)},
                   'reporter': {str(c): rep.get(c, 0) for c in range(111)}}, open(out, 'w'), indent=1)
        print("  per-chip counts -> %s" % out)


def main():
    if len(sys.argv) < 2:
        print(__doc__); return 2
    path = sys.argv[1]; shuf = '--shuffle' in sys.argv
    jobs_path = None
    if '--jobs' in sys.argv:
        jobs_path = sys.argv[sys.argv.index('--jobs') + 1]
    print("=== verifier control ===")
    if not control():
        print("\n*** CONTROL FAILED -- the verifier is wrong. Do not interpret results. ***")
        return 1
    rx = open(path, 'rb').read()
    fr = scan(rx)
    jobs = load_jobs(jobs_path)
    ev, ver = verify(jobs, fr, shuffle=shuf)
    echo = sum(1 for f in fr if f[9] > 0xBF)
    print("\n=== %s%s%s ===" % (os.path.basename(path), "  [SHUFFLED - expect 0]" if shuf else "",
                              ("  jobs=" + os.path.basename(jobs_path)) if jobs_path else ""))
    print("  RX %d B | CRC-OK 04 06 %d | echo %d | nonce-class %d -> %d distinct events"
          % (len(rx), len(fr), echo, len(fr) - echo, len(ev)))
    print("  *** SHA256d-VERIFIED: %d / %d distinct (%s) ***"
          % (len(ver), len(ev), ("%.0f%%" % (100 * len(ver) / len(ev))) if ev else "n/a"))
    if '--per-chip' in sys.argv and ver:
        per_chip(ev, ver, path)
    if ver:
        zs = sorted(v[0] for v in ver)
        print("  leading-zero bits: %s" % zs)
        for v in sorted(ver, reverse=True)[:6]:
            print("     %2d bits  %s  mid %d  job 0x%02X  %d chips" % v)
    return 0


if __name__ == '__main__':
    sys.exit(main())
