#!/usr/bin/env python3
"""Flag weak / dead ASICs from `verify_nonces.py --per-chip OUT.json` output.

    perchip_flag.py OUT.json [--k 5]

Uses the OWNER attribution (nonce slice = floor(nonce / (2^32/111))), which is valid only when the
run sent the §7.2b nonce-base sweep — kf1950_rearm_replay.py does, every cycle, unless KF_SOLO is set.
Each chip's verified count is Poisson around the board mean; a chip is flagged when it sits more
than k sigma below (or above) that mean. Needs a mean of >= ~50/chip (~5,500 verified) to see a
chip at half rate; below that only dead chips are detectable.
"""
import sys, json, math, argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('json'); ap.add_argument('--k', type=float, default=5.0)
    a = ap.parse_args()
    d = json.load(open(a.json))
    own = {int(c): n for c, n in d['owner'].items()}
    chips = sorted(own)
    tot = sum(own.values()); n = len(chips); mean = tot / n
    sd_obs = math.sqrt(sum((own[c] - mean) ** 2 for c in chips) / (n - 1))
    print('%s: %d verified, %d chips, mean %.1f/chip, observed sd %.1f vs Poisson %.1f'
          % (d.get('file', a.json), tot, n, mean, sd_obs, math.sqrt(mean)))
    print('owner-among-reporters agreement: %s/%s' % (d.get('agree'), d.get('verified')))
    if mean < 20:
        print('⚠ mean < 20/chip: too few nonces to judge individual chips beyond "zero" -- run longer')
    lo = mean - a.k * math.sqrt(mean); hi = mean + a.k * math.sqrt(mean)
    dead = [c for c in chips if own[c] == 0]
    weak = [c for c in chips if 0 < own[c] < lo]
    hot = [c for c in chips if own[c] > hi]
    for c in dead:
        print('  DEAD   chip 0x%02X (%3d): 0 nonces' % (c, c))
    for c in weak:
        print('  WEAK   chip 0x%02X (%3d): %d (%.0f%% of mean, %.1f sigma)' % (c, c, own[c], 100 * own[c] / mean, (own[c] - mean) / math.sqrt(mean)))
    for c in hot:
        print('  HIGH   chip 0x%02X (%3d): %d (+%.1f sigma -- check the nonce-base sweep landed; overlapping slices?)' % (c, c, own[c], (own[c] - mean) / math.sqrt(mean)))
    if sd_obs > 2 * math.sqrt(mean):
        print('⚠ spread is %.1fx Poisson -- board-wide non-uniformity (thermal gradient, string voltage imbalance)' % (sd_obs / math.sqrt(mean)))
    if not (dead or weak or hot):
        print('  PASS: every chip within %.0f sigma of the board mean (%.0f-%.0f)' % (a.k, max(lo, 0), hi))
    return 1 if dead or weak else 0


if __name__ == '__main__':
    sys.exit(main())
