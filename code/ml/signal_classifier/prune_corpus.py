#!/usr/bin/env python3
"""Apply the current signal-presence gate to samples already on disk.

The collector's gate only affects what it writes NEXT. Samples captured before a
gate existed — or before it was tightened — stay in the corpus and quietly train
the model on empty channels. This re-checks everything against the live
threshold and moves failures to quarantine rather than deleting them, so the
judgement stays reviewable.

Usage:
    python3 prune_corpus.py --data-dir data/collected --dry-run
    python3 prune_corpus.py --data-dir data/collected
"""

import argparse
import glob
import os
import shutil
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "ravensdr"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", ".."))

try:
    from ravensdr.signal_classifier import (COLLECT_MIN_PEAK_RATIO,
                                            spectral_peak_ratio)
except ImportError:                                   # running detached from the app
    COLLECT_MIN_PEAK_RATIO = 300.0

    def spectral_peak_ratio(iq, nfft=8192):
        seg = np.asarray(iq[:nfft])
        if len(seg) < 64:
            return 0.0
        p = np.abs(np.fft.fft(seg)) ** 2
        med = float(np.median(p))
        if med <= 0:
            return 0.0
        p[0] = med
        return float(p.max() / med)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/collected")
    ap.add_argument("--quarantine", default="data/quarantine/failed-gate")
    ap.add_argument("--threshold", type=float, default=COLLECT_MIN_PEAK_RATIO)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("Gate: spectral peak/median >= %.0f\n" % args.threshold)
    print("%-10s %7s %7s %7s   %s" % ("class", "total", "keep", "drop", "median kept"))
    grand_keep = grand_drop = 0

    for cls in sorted(os.listdir(args.data_dir)):
        cdir = os.path.join(args.data_dir, cls)
        if not os.path.isdir(cdir):
            continue
        files = sorted(glob.glob(os.path.join(cdir, "*.npy")))
        if not files:
            continue

        kept_ratios, drop = [], []
        for path in files:
            try:
                r = spectral_peak_ratio(np.load(path))
            except Exception:
                drop.append(path)
                continue
            (kept_ratios.append(r) if r >= args.threshold else drop.append(path))

        if drop and not args.dry_run:
            dest = os.path.join(args.quarantine, cls)
            os.makedirs(dest, exist_ok=True)
            for path in drop:
                shutil.move(path, os.path.join(dest, os.path.basename(path)))

        grand_keep += len(kept_ratios)
        grand_drop += len(drop)
        print("%-10s %7d %7d %7d   %11.0f"
              % (cls, len(files), len(kept_ratios), len(drop),
                 np.median(kept_ratios) if kept_ratios else 0))

    print("\n%s %d kept, %d %s"
          % ("WOULD KEEP" if args.dry_run else "kept", grand_keep, grand_drop,
             "would be quarantined" if args.dry_run else "quarantined"))
    if grand_keep:
        print("corpus is %.0f%% of its former size" %
              (100 * grand_keep / (grand_keep + grand_drop)))


if __name__ == "__main__":
    main()
