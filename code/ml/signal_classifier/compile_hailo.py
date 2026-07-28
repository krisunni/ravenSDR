#!/usr/bin/env python3
"""Compile signal_classifier.onnx to a Hailo-8L .hef.

Runs INSIDE the Hailo AI SW Suite container, on x86. The Dataflow Compiler is
x86-only, which is the whole reason the classifier has been running on the Pi's
CPU through onnxruntime instead of the NPU.

`hailomz` is not the tool for this. The Model Zoo CLI only handles zoo models;
a custom ONNX goes through the Dataflow Compiler's Python API directly.

Three stages, each saved so a failure does not cost the previous one:

    translate  ONNX -> HAR          (graph only; fast)
    optimize   HAR  -> quantized    (int8; needs the calibration set; slow)
    compile    HAR  -> .hef         (allocation onto the NPU's resources)

Usage, inside the suite:
    python3 compile_hailo.py \\
        --onnx signal_classifier.onnx \\
        --calib calibration.npy \\
        --out signal_classifier_h8l.hef
"""

import argparse
import os
import sys

import numpy as np

# The Pi AI Hat carries a Hailo-8L (13 TOPS), NOT a Hailo-8 (26 TOPS). They are
# different targets: a .hef built for hailo8 will not load on hailo8l, and the
# failure is at load time on the Pi, long after the hour spent compiling.
DEFAULT_ARCH = "hailo8l"

# Normalisation happens on-chip rather than on the host. The .hef then accepts
# the raw uint8 spectrogram bytes the collector already produces, and the Pi
# does not spend cycles building a float array per inference.
#
# NOTE: this changes what the runtime must send. signal_classifier._infer_hailo
# currently divides by 255 before handing the buffer over; with this layer in
# the graph it must pass uint8 through untouched, or every value is scaled twice
# and the model sees near-black images. See --no-onchip-norm to keep the host
# doing it instead.
# The first compile agreed with the CPU model only 53% of the time. It was not
# a structural error — on an OOK window the NPU still picked OOK, but at 0.61
# against the CPU's 0.9985. The probabilities were smeared, which flips every
# borderline window. Cause: fifteen logits quantised to 8 bits leaves too little
# resolution for softmax to separate them.
#
# So the classifier head runs at 16-bit while the convolutional trunk stays at
# 8-bit — the trunk is where the compute is, the head is where the precision
# matters. Bias correction (optimization_level=1) recovers what per-layer
# clipping costs; level 2 adds finetuning, which needs a GPU this VM lacks.
MODEL_SCRIPT = """
normalization1 = normalization([0.0, 0.0, 0.0], [255.0, 255.0, 255.0])
quantization_param([fc1], precision_mode=a16_w16)
model_optimization_flavor(optimization_level=1, compression_level=0)
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--onnx", default="signal_classifier.onnx")
    ap.add_argument("--calib", default="calibration.npy")
    ap.add_argument("--out", default="signal_classifier_h8l.hef")
    ap.add_argument("--arch", default=DEFAULT_ARCH)
    ap.add_argument("--name", default="signal_classifier")
    ap.add_argument("--har-dir", default=".")
    ap.add_argument("--no-onchip-norm", action="store_true",
                    help="leave normalisation on the host; calibration must "
                         "then be float 0..1, not uint8")
    args = ap.parse_args()

    try:
        from hailo_sdk_client import ClientRunner
    except ImportError:
        print("hailo_sdk_client not importable — this must run inside the "
              "Hailo AI SW Suite container on x86.")
        return 1

    for path in (args.onnx, args.calib):
        if not os.path.exists(path):
            print("missing: %s" % path)
            return 1

    calib = np.load(args.calib)
    print("calibration: shape %s dtype %s range %d..%d"
          % (calib.shape, calib.dtype, calib.min(), calib.max()))
    if calib.ndim != 4 or calib.shape[-1] != 3:
        print("expected NHWC with 3 channels, got %s" % (calib.shape,))
        return 1

    runner = ClientRunner(hw_arch=args.arch)
    print("\n[1/3] translating %s for %s" % (args.onnx, args.arch))
    # torch.onnx.export writes NCHW. The translator is told the shape in the
    # ONNX's own layout; the calibration set stays NHWC, which is what the
    # optimizer and the runtime both use.
    runner.translate_onnx_model(
        args.onnx,
        args.name,
        net_input_shapes={"input": [1, 3, 224, 224]},
    )
    har = os.path.join(args.har_dir, args.name + ".har")
    runner.save_har(har)
    print("      -> %s" % har)

    if not args.no_onchip_norm:
        print("\n[2/3] optimizing (int8) with on-chip normalisation")
        runner.load_model_script(MODEL_SCRIPT)
    else:
        print("\n[2/3] optimizing (int8), host-side normalisation")
        calib = calib.astype(np.float32) / 255.0

    # No GPU on the training VM, so this runs on CPU and is the slow stage.
    runner.optimize(calib)
    quantized = os.path.join(args.har_dir, args.name + "_quantized.har")
    runner.save_har(quantized)
    print("      -> %s" % quantized)

    print("\n[3/3] compiling")
    hef = runner.compile()
    with open(args.out, "wb") as f:
        f.write(hef)
    print("      -> %s (%.1f MB)" % (args.out, len(hef) / 1e6))

    print("\nNext: copy %s to code/ravensdr/models/ on the Pi." % args.out)
    if not args.no_onchip_norm:
        print("The .hef normalises on-chip, so _infer_hailo must send uint8 "
              "and stop dividing by 255.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
