#!/usr/bin/env python3
"""Compare the Hailo NPU against the ONNX CPU model on identical inputs.

"The NPU path runs" is not the same as "the NPU path is right". Quantising to
int8 can shift predictions, and the failure is quiet: the console keeps
reporting classifications and nothing looks broken. The only way to know is to
push the same spectrograms through both and compare.

Run on the Pi, with the .hef in place:
    python3 compare_backends.py --n 200
"""

import argparse
import glob
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "code"))
sys.path.insert(0, HERE)

from dataset import (RUNTIME_SAMPLE_LEN, iq_to_spectrogram,  # noqa: E402
                     spectrogram_to_image)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir",
                    default=os.path.join(HERE, "data", "collected"))
    ap.add_argument("--hef", default=os.path.join(
        ROOT, "code", "ravensdr", "models", "signal_classifier_h8l.hef"))
    ap.add_argument("--onnx", default=os.path.join(
        ROOT, "code", "ravensdr", "models", "signal_classifier.onnx"))
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    from ravensdr.signal_classifier import SignalClassifier

    classes_path = os.path.join(ROOT, "code", "ravensdr", "models",
                                "signal_classifier_classes.json")

    # Two independent instances so each is pinned to one backend, rather than
    # trusting a preference order to have picked what we think it picked.
    npu = SignalClassifier(hef_path=args.hef, class_map_path=classes_path,
                           onnx_path=None)
    cpu = SignalClassifier(hef_path=None, class_map_path=classes_path,
                           onnx_path=args.onnx)
    print("npu backend: %s | cpu backend: %s" % (npu.backend, cpu.backend))
    if npu.backend != "hailo":
        print("NPU did not initialise — nothing to compare.")
        return 1

    rng = np.random.default_rng(args.seed)
    files = []
    for cls in sorted(os.listdir(args.data_dir)):
        d = os.path.join(args.data_dir, cls)
        if os.path.isdir(d):
            files += sorted(glob.glob(os.path.join(d, "*.npy")))
    if not files:
        print("no samples under %s" % args.data_dir)
        return 1
    pick = rng.permutation(len(files))[:args.n]

    agree = 0
    total = 0
    npu_ms = []
    cpu_ms = []
    disagreements = []

    for i in pick:
        try:
            iq = np.load(files[i])
        except Exception:
            continue
        if not np.iscomplexobj(iq) or len(iq) < RUNTIME_SAMPLE_LEN:
            continue
        img = spectrogram_to_image(iq_to_spectrogram(iq[:RUNTIME_SAMPLE_LEN]))

        t = time.perf_counter()
        a = npu._infer_hailo(img)
        npu_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        b = cpu._infer_onnx(img)
        cpu_ms.append((time.perf_counter() - t) * 1000)

        if not a or not b:
            continue
        total += 1
        if a[0] == b[0]:
            agree += 1
        elif len(disagreements) < 12:
            disagreements.append((os.path.basename(files[i]),
                                  b[0], b[1], a[0], a[1]))

    if not total:
        print("no comparable inferences")
        return 1

    print("\ncompared %d windows" % total)
    print("agreement          %.1f%%  (%d/%d)"
          % (100.0 * agree / total, agree, total))
    print("NPU latency        %.1f ms median, %.1f ms p95"
          % (np.median(npu_ms), np.percentile(npu_ms, 95)))
    print("CPU latency        %.1f ms median, %.1f ms p95"
          % (np.median(cpu_ms), np.percentile(cpu_ms, 95)))
    print("speedup            %.1fx" % (np.median(cpu_ms) / max(np.median(npu_ms), 1e-6)))

    if disagreements:
        print("\ndisagreements (cpu -> npu):")
        for name, bc, bp, ac, apr in disagreements:
            print("  %-44s %-9s %.2f  ->  %-9s %.2f"
                  % (name[:44], bc, bp, ac, apr))

    # Quantisation moving a few borderline windows is expected. Wholesale
    # disagreement means the input format is wrong — most likely the
    # normalisation being applied twice, or NCHW fed where NHWC was wanted.
    if agree / total < 0.9:
        print("\nAgreement below 90%: treat the NPU path as WRONG, not merely "
              "degraded. Check whether the .hef normalises on-chip while the "
              "runtime is also dividing by 255.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
