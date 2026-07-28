#!/usr/bin/env python3
"""Export trained PyTorch signal classifier to ONNX format.

Exports with fixed input shape (1, 3, 224, 224) float32, opset 13
for compatibility with Hailo DFC v3.x.
"""

import argparse
import json
import os
import sys

import numpy as np

try:
    import torch
    from torchvision import models
    import torch.nn as nn
except ImportError:
    print("ERROR: PyTorch required — pip install torch torchvision")
    sys.exit(1)

try:
    import onnx
    import onnxruntime as ort
except ImportError:
    print("ERROR: ONNX required — pip install onnx onnxruntime")
    sys.exit(1)


def run_export(args):
    # Load checkpoint
    print(f"Loading checkpoint from {args.checkpoint}...")
    checkpoint = torch.load(args.checkpoint, map_location="cpu")
    num_classes = checkpoint.get("num_classes", 11)

    # Rebuild model
    model = models.mobilenet_v2(weights=None)
    model.classifier[1] = nn.Linear(model.last_channel, num_classes)
    model.load_state_dict(checkpoint["model_state_dict"])
    model.train(False)

    # Export to ONNX
    dummy_input = torch.randn(1, 3, 224, 224)
    os.makedirs(args.output_dir, exist_ok=True)
    onnx_path = os.path.join(args.output_dir, "signal_classifier.onnx")

    print(f"Exporting to ONNX (opset {args.opset})...")
    # dynamo=False forces the legacy TorchScript exporter.
    #
    # torch 2.13 defaults to the dynamo exporter, which drags in onnxscript ->
    # onnx_ir -> ml_dtypes and fails on this box with
    # "module 'ml_dtypes' has no attribute 'bfloat16'" — a version conflict
    # inside that chain, not a problem with the model. The legacy exporter has
    # none of those dependencies and produces exactly the fixed-shape NCHW graph
    # the Hailo compiler expects, so it is the right tool here regardless.
    export_kwargs = dict(
        opset_version=args.opset,
        input_names=["input"],
        output_names=["output"],
        dynamic_axes=None,  # fixed shape
    )
    try:
        torch.onnx.export(model, dummy_input, onnx_path,
                          dynamo=False, **export_kwargs)
    except TypeError:
        # older torch has no dynamo kwarg — it only had the legacy exporter
        torch.onnx.export(model, dummy_input, onnx_path, **export_kwargs)

    # Verify ONNX model
    print("Verifying ONNX model...")
    onnx_model = onnx.load(onnx_path)
    onnx.checker.check_model(onnx_model)

    # Compare outputs
    print("Comparing PyTorch vs ONNX outputs...")
    test_input = np.random.randn(1, 3, 224, 224).astype(np.float32)

    with torch.no_grad():
        pytorch_output = model(torch.from_numpy(test_input)).numpy()

    session = ort.InferenceSession(onnx_path)
    onnx_output = session.run(None, {"input": test_input})[0]

    max_diff = np.max(np.abs(pytorch_output - onnx_output))
    print(f"Max absolute difference: {max_diff:.2e}")

    if max_diff > 1e-5:
        print(f"WARNING: Difference {max_diff:.2e} exceeds tolerance 1e-5")
    else:
        print("ONNX verification passed")

    # Copy class mapping alongside model
    class_map_src = os.path.join(os.path.dirname(args.checkpoint), "..",
                                  "data", "dataset_classes.json")

    class_map_dst = os.path.join(args.output_dir, "signal_classifier_classes.json")
    if os.path.exists(class_map_src):
        import shutil
        shutil.copy2(class_map_src, class_map_dst)
        print(f"Class mapping copied to {class_map_dst}")

    print(f"\nONNX model saved to: {onnx_path}")
    print(f"Input shape: (1, 3, 224, 224) float32")
    print(f"Output shape: (1, {num_classes}) float32")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export signal classifier to ONNX")
    parser.add_argument("--checkpoint", type=str, required=True,
                        help="Path to best_model.pth")
    parser.add_argument("--output_dir", type=str, default="exports")
    parser.add_argument("--opset", type=int, default=13)
    args = parser.parse_args()
    run_export(args)
