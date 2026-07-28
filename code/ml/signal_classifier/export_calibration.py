#!/usr/bin/env python3
"""Export a calibration set for Hailo int8 quantisation.

Quantisation picks per-tensor scales from whatever data you show it. If that
data does not look like production traffic the scales are wrong, and the model
loses accuracy on the NPU for reasons that are invisible from the graph. So this
does not synthesise anything: it replays real captures from this node's own
corpus through the *exact* preprocessing the runtime uses.

Matches signal_classifier._infer_hailo():
    224x224 grayscale spectrogram -> stacked to 3 identical channels -> NHWC.

Written as uint8. Hailo's optimizer takes the normalisation as a layer
(`normalization([0,0,0], [255,255,255])`), which keeps the division off the
host and means the .hef accepts the same bytes the ONNX path already produces.

Class balance matters here too: the corpus is skewed roughly 2.5:1, and
calibrating mostly on the majority class biases the scales toward it.

Usage:
    python3 export_calibration.py --data-dir data/collected --n 1024
"""

import argparse
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dataset import (RUNTIME_SAMPLE_LEN, iq_to_spectrogram,  # noqa: E402
                     spectrogram_to_image)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/collected")
    ap.add_argument("--out", default="calibration.npy")
    ap.add_argument("--n", type=int, default=1024,
                    help="total windows; Hailo's guidance is 64-1024")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    classes = sorted(d for d in os.listdir(args.data_dir)
                     if os.path.isdir(os.path.join(args.data_dir, d)))
    if not classes:
        print("No class directories under %s" % args.data_dir)
        return 1

    per_class = max(1, args.n // len(classes))
    print("%d classes, taking %d windows each\n" % (len(classes), per_class))

    images = []
    for cls in classes:
        files = sorted(glob.glob(os.path.join(args.data_dir, cls, "*.npy")))
        if not files:
            print("  %-10s no samples — skipped" % cls)
            continue
        pick = rng.permutation(len(files))[:per_class]
        kept = 0
        for i in pick:
            try:
                iq = np.load(files[i])
            except Exception:
                continue
            if not np.iscomplexobj(iq) or len(iq) < RUNTIME_SAMPLE_LEN:
                continue
            img = spectrogram_to_image(
                iq_to_spectrogram(iq[:RUNTIME_SAMPLE_LEN]))
            # Three identical channels: the network is ImageNet-pretrained and
            # expects RGB, exactly as the runtime feeds it.
            images.append(np.stack([img, img, img], axis=-1))
            kept += 1
        print("  %-10s %4d windows from %d files" % (cls, kept, len(files)))

    if not images:
        print("Nothing usable found.")
        return 1

    arr = np.asarray(images, dtype=np.uint8)
    np.save(args.out, arr)
    print("\nshape %s  dtype %s  %.1f MB -> %s"
          % (arr.shape, arr.dtype, arr.nbytes / 1e6, args.out))
    print("range %d..%d  mean %.1f" % (arr.min(), arr.max(), arr.mean()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
