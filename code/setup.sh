#!/usr/bin/env bash
# ravenSDR — One-shot system dependency installer
# Target: Raspberry Pi OS (Bookworm, 64-bit)
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; }

echo "============================================"
echo "  ravenSDR — System Setup"
echo "============================================"
echo ""

# ── Step 1: Check platform ──
echo "── Step 1: Platform check ──"
if [ -f /etc/rpi-issue ]; then
    pass "Raspberry Pi OS detected"
else
    warn "Not running on Raspberry Pi OS — some features may not work"
fi

# ── Step 2: Install system packages ──
echo ""
echo "── Step 2: System packages ──"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    rtl-sdr \
    librtlsdr-dev \
    sox \
    alsa-utils \
    ffmpeg \
    python3-venv \
    python3-pip

# Try rtl-biast (may not be in default repos)
if sudo apt-get install -y -qq rtl-biast 2>/dev/null; then
    pass "rtl-biast installed"
else
    warn "rtl-biast not available in repos — bias tee control unavailable"
fi

pass "System packages installed"

# ── Step 3: ALSA loopback module ──
echo ""
echo "── Step 3: ALSA loopback ──"
if sudo modprobe snd-aloop 2>/dev/null; then
    pass "snd-aloop module loaded"
    if ! grep -q "snd-aloop" /etc/modules 2>/dev/null; then
        echo "snd-aloop" | sudo tee -a /etc/modules > /dev/null
        pass "snd-aloop persisted in /etc/modules"
    fi
else
    warn "snd-aloop not available — may need kernel headers"
fi

# ── Step 4: Blacklist DVB kernel module ──
echo ""
echo "── Step 4: DVB blacklist ──"
BLACKLIST_FILE="/etc/modprobe.d/rtlsdr.conf"
if [ ! -f "$BLACKLIST_FILE" ]; then
    echo "blacklist dvb_usb_rtl28xxu" | sudo tee "$BLACKLIST_FILE" > /dev/null
    pass "DVB module blacklisted"
else
    pass "DVB blacklist already exists"
fi

# ── Step 5: Python venv ──
echo ""
echo "── Step 5: Python environment ──"
VENV_DIR="$(dirname "$0")/../.venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
    pass "Virtual environment created at $VENV_DIR"
else
    pass "Virtual environment already exists"
fi

source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q

echo "Installing Python packages (torch is large, this may take a few minutes)..."
pip install -r "$(dirname "$0")/requirements.txt"
pip install -e "$(dirname "$0")" -q
pass "Python packages installed"

# ── Step 6: dump1090 for ADS-B (optional) ──
echo ""
echo "── Step 6: dump1090 (ADS-B) ──"
if command -v dump1090-mutability &>/dev/null; then
    pass "dump1090-mutability already installed"
else
    if sudo apt-get install -y -qq dump1090-mutability 2>/dev/null; then
        pass "dump1090-mutability installed from apt"
    else
        warn "dump1090 not in apt — ADS-B features will be unavailable"
        warn "To install manually: git clone https://github.com/flightaware/dump1090.git && cd dump1090 && make"
    fi
fi

# ── Step 7: Test RTL-SDR ──
echo ""
echo "── Step 7: RTL-SDR test ──"
if command -v rtl_test &>/dev/null; then
    if timeout 5 rtl_test -t 2>&1 | grep -q "R828D"; then
        pass "RTL-SDR Blog V4 detected (R828D tuner)"
    else
        warn "RTL-SDR not detected — web stream mode will be used"
    fi
else
    warn "rtl_test not found — rtl-sdr package may not be installed"
fi

# ── Step 8: Bias tee ──
echo ""
echo "── Step 8: Bias tee check ──"
if command -v rtl_biast &>/dev/null; then
    rtl_biast -b 0 2>/dev/null && pass "Bias tee disabled" || warn "Bias tee command failed"
else
    warn "rtl_biast not available — skipping"
fi

# ── Step 9: Hailo SDK + Models ──
echo ""
echo "── Step 9: Hailo SDK ──"
if command -v hailortcli &>/dev/null; then
    if hailortcli fw-control identify 2>/dev/null; then
        pass "Hailo NPU detected"

        # Symlink hailo_platform into venv (installed system-wide by hailort deb)
        SITE_PKGS=$("$VENV_DIR/bin/python3" -c "import site; print(site.getsitepackages()[0])")
        if [ ! -e "$SITE_PKGS/hailo_platform" ] && [ -d /usr/lib/python3/dist-packages/hailo_platform ]; then
            ln -s /usr/lib/python3/dist-packages/hailo_platform "$SITE_PKGS/hailo_platform"
            pass "hailo_platform symlinked into venv"
        fi

        # Check if model files exist, download if not
        MODELS_DIR="$(dirname "$0")/ravensdr/models"
        if [ -f "$MODELS_DIR/h8l/tiny-whisper-encoder-10s_15dB_h8l.hef" ] && \
           [ -f "$MODELS_DIR/h8l/tiny-whisper-decoder-fixed-sequence-matmul-split_h8l.hef" ] && \
           [ -f "$MODELS_DIR/decoder_assets/token_embedding_weight_tiny.npy" ] && \
           [ -f "$MODELS_DIR/decoder_assets/onnx_add_input_tiny.npy" ]; then
            pass "Hailo Whisper model files present"
        else
            warn "Hailo model files missing — downloading..."
            bash "$(dirname "$0")/scripts/download_models.sh"
            pass "Hailo Whisper model files downloaded"
        fi
    else
        warn "Hailo SDK installed but NPU not detected — CPU fallback will be used"
    fi
else
    warn "Hailo SDK not installed — CPU fallback will be used (faster-whisper)"
fi

# ── Step 10: Summary ──
echo ""
echo "============================================"
echo "  Setup Complete"
echo "============================================"
echo ""
echo "To start ravenSDR:"
echo "  source $VENV_DIR/bin/activate"
echo "  python3 -m ravensdr.app"
echo ""
