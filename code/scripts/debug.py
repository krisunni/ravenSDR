#!/usr/bin/env python3
"""ravenSDR doctor — end-to-end health check for the Hailo transcription stack.

Run with the project venv:  /home/kris/code/ravenSDR/.venv/bin/python3 code/scripts/debug.py

Answers the question "is Hailo really going to work?" by checking the device,
driver params, venv, dependencies, model files, and by running a real one-shot
encoder inference on the NPU. Exits non-zero if any critical check fails.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(HERE)                 # .../ravenSDR/code
MODELS = os.path.join(CODE_DIR, "ravensdr", "models")
sys.path.insert(0, CODE_DIR)

GREEN, RED, YELLOW, NC = "\033[0;32m", "\033[0;31m", "\033[1;33m", "\033[0m"
_fail = {"n": 0}


def ok(msg):
    print(f"{GREEN}[PASS]{NC} {msg}")


def bad(msg, critical=True):
    print(f"{RED if critical else YELLOW}[{'FAIL' if critical else 'WARN'}]{NC} {msg}")
    if critical:
        _fail["n"] += 1


def check_device():
    if os.path.exists("/dev/hailo0"):
        ok("/dev/hailo0 present")
    else:
        bad("/dev/hailo0 missing — hailo_pci not loaded / no device")


def check_identify():
    try:
        out = subprocess.run(["hailortcli", "fw-control", "identify"],
                             capture_output=True, text=True, timeout=15).stdout
        arch = next((l.split(":")[1].strip() for l in out.splitlines()
                     if "Device Architecture" in l), "?")
        ok(f"hailortcli identify OK (arch {arch})")
    except Exception as e:
        bad(f"hailortcli identify failed: {e}")


def check_desc_page_size():
    p = "/sys/module/hailo_pci/parameters/force_desc_page_size"
    try:
        val = open(p).read().strip()
    except OSError:
        bad("hailo_pci not loaded (can't read force_desc_page_size)")
        return
    if val == "4096":
        ok("force_desc_page_size=4096 (required on 16KB-page kernels)")
    else:
        page = os.sysconf("SC_PAGE_SIZE")
        if page > 4096:
            bad(f"force_desc_page_size={val} but PAGE_SIZE={page} — configure() will fail; "
                f"set /etc/modprobe.d/hailo_pci.conf and reload")
        else:
            bad(f"force_desc_page_size={val} (PAGE_SIZE={page})", critical=False)


def check_python_and_numpy():
    ok(f"venv python {sys.version.split()[0]}")
    try:
        import numpy
        major = int(numpy.__version__.split(".")[0])
        (ok if major == 1 else bad)(f"numpy {numpy.__version__} "
                                    f"({'1.x OK' if major == 1 else 'must be 1.x for pyhailort ABI'})")
    except Exception as e:
        bad(f"numpy import failed: {e}")


def check_imports():
    for mod, critical in [("hailo_platform", True), ("transformers", True), ("faster_whisper", True)]:
        try:
            __import__(mod)
            ok(f"import {mod}")
        except Exception as e:
            bad(f"import {mod} failed: {e}", critical=critical)
    # ravensdr.mel must be pure numpy — check in isolation (other libs like
    # transformers may pull torch into this process, which would mask a regression).
    probe = ("import sys; import ravensdr.mel; "
             "sys.exit(1 if 'torch' in sys.modules else 0)")
    r = subprocess.run([sys.executable, "-c", probe], cwd=CODE_DIR,
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok("ravensdr.mel is torch-free")
    elif r.returncode == 1:
        bad("ravensdr.mel imports torch (should be pure numpy)", critical=False)
    else:
        bad(f"import ravensdr.mel failed: {r.stderr.strip().splitlines()[-1:] or r.stderr}")


def check_model_files():
    files = [
        os.path.join(MODELS, "h8l", "tiny-whisper-encoder-10s_15dB_h8l.hef"),
        os.path.join(MODELS, "h8l", "tiny-whisper-decoder-fixed-sequence-matmul-split_h8l.hef"),
        os.path.join(MODELS, "decoder_assets", "token_embedding_weight_tiny.npy"),
        os.path.join(MODELS, "decoder_assets", "onnx_add_input_tiny.npy"),
        os.path.join(MODELS, "mel_filters.npz"),
    ]
    missing = [f for f in files if not os.path.exists(f)]
    if not missing:
        ok(f"all {len(files)} model files present")
    else:
        for m in missing:
            bad(f"missing model file: {os.path.relpath(m, CODE_DIR)} — run scripts/download_models.sh")


def check_tokenizer_offline():
    try:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from transformers import AutoTokenizer
        AutoTokenizer.from_pretrained("openai/whisper-tiny")
        ok("whisper-tiny tokenizer loads from local cache (offline)")
    except Exception as e:
        bad(f"tokenizer not cached — run scripts/download_models.sh --hf-cache ({e})", critical=False)
    finally:
        os.environ.pop("HF_HUB_OFFLINE", None)


def check_hailo_smoke():
    """Real one-shot encoder inference on the NPU."""
    try:
        import numpy as np
        from ravensdr.mel import log_mel_spectrogram, pad_or_trim, CHUNK_LENGTH_S, SAMPLE_RATE
        from hailo_platform import HEF, VDevice, HailoSchedulingAlgorithm, FormatType  # noqa: F401
    except Exception as e:
        bad(f"Hailo smoke skipped — import failed: {e}")
        return

    enc = os.path.join(MODELS, "h8l", "tiny-whisper-encoder-10s_15dB_h8l.hef")
    if not os.path.exists(enc):
        bad("Hailo smoke skipped — encoder HEF missing")
        return
    try:
        n = CHUNK_LENGTH_S * SAMPLE_RATE
        t = np.arange(n) / SAMPLE_RATE
        audio = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        mel = log_mel_spectrogram(pad_or_trim(audio, n))
        mel_np = np.transpose(np.expand_dims(np.expand_dims(mel, 0), 2), (0, 2, 3, 1))
        input_mel = np.ascontiguousarray(mel_np).astype(np.float32)

        params = VDevice.create_params()
        params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
        with VDevice(params) as vdev:
            model = vdev.create_infer_model(enc)
            model.input().set_format_type(FormatType.FLOAT32)
            model.output().set_format_type(FormatType.FLOAT32)
            with model.configure() as cfg:
                b = cfg.create_bindings()
                b.input().set_buffer(input_mel)
                out = np.zeros(model.output().shape, dtype=np.float32)
                b.output().set_buffer(out)
                cfg.run([b], 10000)
                feats = b.output().get_buffer()
        if np.count_nonzero(feats):
            ok(f"Hailo encoder inference ran on NPU (out {feats.shape})")
        else:
            bad("Hailo encoder ran but produced all-zero output")
    except Exception as e:
        bad(f"Hailo encoder inference failed: {e}")


def main():
    print("=== ravenSDR doctor ===\n")
    print("- Device & driver -")
    check_device(); check_identify(); check_desc_page_size()
    print("\n- Python environment -")
    check_python_and_numpy(); check_imports()
    print("\n- Models -")
    check_model_files(); check_tokenizer_offline()
    print("\n- Hailo inference -")
    check_hailo_smoke()
    print()
    if _fail["n"]:
        print(f"{RED}{_fail['n']} critical check(s) FAILED{NC}")
        sys.exit(1)
    print(f"{GREEN}All critical checks passed — Hailo backend is ready.{NC}")


if __name__ == "__main__":
    main()
