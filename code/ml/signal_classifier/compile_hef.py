#!/usr/bin/env python3
"""Compile ONNX signal classifier to Hailo HEF format.

Uses Hailo Dataflow Compiler (DFC) v3.31+ to compile for HAILO8L target.
Must run on x86 Ubuntu 22 with DFC installed — cannot compile on Pi.

Post-training quantization with calibration dataset of representative
spectrograms.
"""

import argparse
import os
import sys

import numpy as np


def compile_hef(args):
    try:
        from hailo_sdk_client import ClientRunner
    except ImportError:
        print("ERROR: Hailo DFC SDK required — install on x86 Ubuntu 22")
        print("See: https://hailo.ai/developer-zone/")
        sys.exit(1)

    onnx_path = args.onnx_model
    if not os.path.exists(onnx_path):
        print(f"ERROR: ONNX model not found: {onnx_path}")
        sys.exit(1)

    print(f"Loading ONNX model: {onnx_path}")
    print(f"Target: HAILO8L")

    # Initialize Hailo client
    runner = ClientRunner(hw_arch="hailo8l")

    # Parse ONNX model
    print("Parsing ONNX model...")
    hn, npz = runner.translate_onnx_model(
        onnx_path,
        net_name="signal_classifier",
        start_node_names=["input"],
        end_node_names=["output"],
    )

    # Load calibration data for quantization
    print("Loading calibration data...")
    if args.calibration_data and os.path.exists(args.calibration_data):
        cal_data = np.load(args.calibration_data)
        cal_images = cal_data["val_images"][:100]  # first 100 validation samples
        # Normalize and replicate to 3 channels
        cal_set = []
        for img in cal_images:
            img_f = img.astype(np.float32) / 255.0
            # NHWC, not NCHW. The ONNX graph is NCHW (PyTorch's layout) and the
            # DFC inserts a transpose for it, but optimize() wants calibration
            # data in the NETWORK INPUT layout, which for HailoRT is NHWC.
            # Feeding (N,3,224,224) here calibrates the quantiser against a
            # transposed image, so the scales come out wrong and the compiled
            # model underperforms for reasons that look like a training problem.
            # Runtime feeds (1,224,224,3) float 0..1 — match it exactly.
            img_3ch = np.stack([img_f, img_f, img_f], axis=-1)   # (224,224,3)
            cal_set.append(np.expand_dims(img_3ch, 0))           # (1,224,224,3)
        calib_dataset = np.concatenate(cal_set, axis=0)
    else:
        print("WARNING: No calibration data — using random data (accuracy may degrade)")
        calib_dataset = np.random.randn(100, 3, 224, 224).astype(np.float32)

    # Optimize model
    print("Optimizing model for HAILO8L...")
    runner.optimize(calib_dataset)

    # Compile to HEF
    os.makedirs(args.output_dir, exist_ok=True)
    hef_path = os.path.join(args.output_dir, "signal_classifier.hef")

    print("Compiling to HEF...")
    hef = runner.compile()

    with open(hef_path, "wb") as f:
        f.write(hef)

    print(f"\nHEF compiled successfully: {hef_path}")
    print(f"File size: {os.path.getsize(hef_path) / 1024:.1f} KB")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compile ONNX to Hailo HEF")
    parser.add_argument("--onnx_model", type=str, required=True,
                        help="Path to signal_classifier.onnx")
    parser.add_argument("--calibration_data", type=str,
                        help="Path to dataset .npz for calibration")
    parser.add_argument("--output_dir", type=str, default="exports")
    args = parser.parse_args()
    compile_hef(args)
