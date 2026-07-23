#!/usr/bin/env bash
# Download model files for ravenSDR.
#   (no args)     download Hailo HEFs + decoder assets from Hailo's S3
#   --hf-cache    pre-cache HuggingFace models (whisper-tiny tokenizer +
#                 faster-whisper tiny) for offline operation, then exit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODELS_DIR="$SCRIPT_DIR/../ravensdr/models"
VENV_PY="$SCRIPT_DIR/../../.venv/bin/python3"
[ -x "$VENV_PY" ] || VENV_PY="python3"

S3_BASE="https://hailo-csdata.s3.eu-west-2.amazonaws.com/resources"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
pass() { echo -e "${GREEN}[OK]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# --- HuggingFace pre-cache (offline mode) ---------------------------------
# Pre-fetch the whisper-tiny tokenizer (Hailo decoder path) and the faster-whisper
# tiny model (CPU fallback) into the default HF cache so the node runs air-gapped
# after first setup. Honors $HF_TOKEN from the environment (never hard-coded) and
# uses hf_transfer for speed when available.
hf_cache() {
    echo "ravenSDR — Pre-caching Whisper models from HuggingFace"
    # Enable hf_transfer only if it is importable, else it would hard-error.
    if "$VENV_PY" -c "import hf_transfer" 2>/dev/null; then
        export HF_HUB_ENABLE_HF_TRANSFER=1
        export HF_XET_HIGH_PERFORMANCE=1
    else
        warn "hf_transfer not installed — downloading at normal speed"
    fi
    [ -n "${HF_TOKEN:-}" ] && echo "Using HF_TOKEN from environment" || warn "No HF_TOKEN set (public models still work, just slower)"

    "$VENV_PY" - <<'PYEOF'
import sys
try:
    from transformers import AutoTokenizer
    AutoTokenizer.from_pretrained("openai/whisper-tiny")
    print("[OK] whisper-tiny tokenizer cached")
except Exception as e:
    print("[WARN] tokenizer cache failed:", e); sys.exit(1)
try:
    from faster_whisper import WhisperModel
    WhisperModel("tiny", device="cpu", compute_type="int8")
    print("[OK] faster-whisper tiny model cached")
except Exception as e:
    print("[WARN] faster-whisper cache failed:", e); sys.exit(1)
PYEOF
}

if [ "${1:-}" = "--hf-cache" ]; then
    hf_cache
    exit $?
fi

echo "ravenSDR — Downloading Hailo-8L Whisper models"
echo ""

# --- HEF files (encoder + decoder) ---
HEF_DIR="$MODELS_DIR/h8l"
mkdir -p "$HEF_DIR"

if [ -f "$HEF_DIR/tiny-whisper-encoder-10s_15dB_h8l.hef" ] && \
   [ -f "$HEF_DIR/tiny-whisper-decoder-fixed-sequence-matmul-split_h8l.hef" ]; then
    pass "HEF files already present"
else
    echo "Downloading encoder HEF..."
    wget -q --show-progress -P "$HEF_DIR" \
        "$S3_BASE/hefs/h8l_rpi/tiny-whisper-encoder-10s_15dB_h8l.hef"

    echo "Downloading decoder HEF..."
    wget -q --show-progress -P "$HEF_DIR" \
        "$S3_BASE/hefs/h8l_rpi/tiny-whisper-decoder-fixed-sequence-matmul-split_h8l.hef"

    pass "HEF files downloaded"
fi

# --- Decoder assets (token embedding + positional bias) ---
ASSETS_DIR="$MODELS_DIR/decoder_assets"
mkdir -p "$ASSETS_DIR"

if [ -f "$ASSETS_DIR/token_embedding_weight_tiny.npy" ] && \
   [ -f "$ASSETS_DIR/onnx_add_input_tiny.npy" ]; then
    pass "Decoder assets already present"
else
    echo "Downloading token embedding weights..."
    wget -q --show-progress -O "$ASSETS_DIR/token_embedding_weight_tiny.npy" \
        "$S3_BASE/npy%20files/whisper/decoder_assets/tiny/decoder_tokenization/token_embedding_weight_tiny.npy"

    echo "Downloading positional bias..."
    wget -q --show-progress -O "$ASSETS_DIR/onnx_add_input_tiny.npy" \
        "$S3_BASE/npy%20files/whisper/decoder_assets/tiny/decoder_tokenization/onnx_add_input_tiny.npy"

    pass "Decoder assets downloaded"
fi

# --- mel_filters.npz (should already be in-tree, but verify) ---
if [ ! -f "$MODELS_DIR/mel_filters.npz" ]; then
    echo "WARNING: mel_filters.npz not found at $MODELS_DIR/mel_filters.npz"
    echo "This file should be checked into the repository."
else
    pass "mel_filters.npz present"
fi

echo ""
echo "All model files ready in $MODELS_DIR"
