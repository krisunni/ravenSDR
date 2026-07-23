#!/usr/bin/env bash
# ravenSDR — One-shot system dependency installer
# Target: Raspberry Pi 5, 64-bit (Raspberry Pi OS Bookworm or Debian Trixie).
# NOTE: Debian Trixie's Pi kernel uses 16KB memory pages, which breaks the Hailo
# driver unless force_desc_page_size=4096 is set (handled in Step 9 below).
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
    sox \
    alsa-utils \
    ffmpeg \
    python3-venv \
    python3-pip \
    cmake \
    build-essential \
    libusb-1.0-0-dev \
    pkg-config

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
# ravenSDR uses a SINGLE venv at repo root: .venv. A stray `venv/` (missing the
# hailo_platform symlink) silently forces CPU fallback — remove it if present.
echo ""
echo "── Step 5: Python environment ──"
STRAY_VENV="$(dirname "$0")/../venv"
if [ -d "$STRAY_VENV" ] && [ -f "$STRAY_VENV/pyvenv.cfg" ]; then
    rm -rf "$STRAY_VENV"
    warn "Removed stray venv/ (use .venv only — it caused CPU fallback)"
fi
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
pip install pyrtlsdr==0.2.93 -q && pass "pyrtlsdr installed (direct IQ capture)" || warn "pyrtlsdr install failed — will use rtl_fm subprocess fallback"
pass "Python packages installed"

# ── Step 6: dump1090 for ADS-B ──
# Debian Trixie has no dump1090 apt package, and the apt 'readsb' is compiled
# WITHOUT RTL-SDR support — so build FlightAware dump1090 from source.
echo ""
echo "── Step 6: dump1090 (ADS-B) ──"
if command -v dump1090-fa &>/dev/null || command -v dump1090 &>/dev/null \
        || command -v dump1090-mutability &>/dev/null; then
    pass "dump1090 already installed"
else
    echo "Building FlightAware dump1090 from source (RTL-SDR ADS-B decoder)..."
    sudo apt-get install -y -qq libncurses-dev librtlsdr-dev 2>/dev/null || true
    D1090_BUILD="/tmp/dump1090-build"
    rm -rf "$D1090_BUILD"
    if git clone --depth 1 https://github.com/flightaware/dump1090 "$D1090_BUILD" 2>/dev/null \
            && make -C "$D1090_BUILD" -j"$(nproc)" RTLSDR=yes BLADERF=no HACKRF=no LIMESDR=no; then
        sudo cp "$D1090_BUILD/dump1090" /usr/local/bin/dump1090-fa
        sudo ln -sf /usr/local/bin/dump1090-fa /usr/local/bin/dump1090
        pass "dump1090-fa built and installed"
    else
        warn "dump1090 build failed — ADS-B features will be unavailable"
    fi
    rm -rf "$D1090_BUILD"
fi

# ── Step 6a: rtl_ais for AIS vessel tracking (optional) ──
echo ""
echo "── Step 6a: rtl_ais (AIS) ──"
if command -v rtl_ais &>/dev/null; then
    pass "rtl_ais already installed"
else
    echo "Building rtl_ais from source..."
    RTLAIS_BUILD_DIR="/tmp/rtl-ais-build"
    rm -rf "$RTLAIS_BUILD_DIR"
    git clone https://github.com/dgiardini/rtl-ais.git "$RTLAIS_BUILD_DIR"
    cd "$RTLAIS_BUILD_DIR"
    make -j"$(nproc)"
    sudo cp rtl_ais /usr/local/bin/
    cd "$OLDPWD"
    rm -rf "$RTLAIS_BUILD_DIR"
    pass "rtl_ais built and installed"
fi

# ── Step 6b: APT Satellite Imaging dependencies ──
echo ""
echo "── Step 6b: APT Satellite Imaging ──"

# Install ephem for satellite pass prediction
pip install ephem -q && pass "ephem installed" || warn "ephem install failed"

# Create APT directories
mkdir -p /tmp/ravensdr/apt
mkdir -p "$(dirname "$0")/static/images/apt"
pass "APT directories created"

# Install an APT image decoder. noaa-apt needs Rust+GTK (heavy on a Pi); aptdec
# is a lightweight C decoder that builds fast — prefer it.
if command -v aptdec &>/dev/null || command -v noaa-apt &>/dev/null; then
    pass "APT decoder already installed"
else
    echo "Building aptdec from source (NOAA APT image decoder)..."
    sudo apt-get install -y -qq libsndfile1-dev libpng-dev cmake 2>/dev/null || true
    APTDEC_BUILD="/tmp/aptdec-build"
    rm -rf "$APTDEC_BUILD"
    if git clone --depth 1 --recurse-submodules https://github.com/Xerbo/aptdec "$APTDEC_BUILD" 2>/dev/null \
            && cmake -S "$APTDEC_BUILD" -B "$APTDEC_BUILD/build" -DCMAKE_BUILD_TYPE=Release >/dev/null 2>&1 \
            && cmake --build "$APTDEC_BUILD/build" -j"$(nproc)" >/dev/null 2>&1; then
        sudo cp "$APTDEC_BUILD/build/aptdec" /usr/local/bin/
        sudo mkdir -p /usr/local/share/aptdec
        sudo cp -r "$APTDEC_BUILD/palettes" /usr/local/share/aptdec/ 2>/dev/null || true
        pass "aptdec built and installed"
    else
        warn "aptdec build failed — APT decoding will be unavailable"
    fi
    rm -rf "$APTDEC_BUILD"
fi

# ── Step 6c: WEFAX Weather Fax dependencies ──
echo ""
echo "── Step 6c: WEFAX Weather Fax ──"

# Install fldigi for WEFAX decoding
if command -v fldigi &>/dev/null; then
    pass "fldigi already installed"
else
    if sudo apt-get install -y -qq fldigi 2>/dev/null; then
        pass "fldigi installed"
    else
        warn "fldigi not in apt — WEFAX decoding will be unavailable"
        warn "To install manually: sudo apt install fldigi"
    fi
fi

# Install Xvfb for headless fldigi operation
if command -v Xvfb &>/dev/null || command -v xvfb-run &>/dev/null; then
    pass "Xvfb already installed"
else
    if sudo apt-get install -y -qq xvfb 2>/dev/null; then
        pass "Xvfb installed (headless display for fldigi)"
    else
        warn "Xvfb not installed — fldigi may not work headlessly"
    fi
fi

# Create WEFAX directories
mkdir -p /tmp/ravensdr/wefax
mkdir -p "$(dirname "$0")/static/images/wefax"
pass "WEFAX directories created"

echo ""
echo "  WEFAX notes:"
echo "  - RTL-SDR Blog V4 supports HF via -D 2 (Q-branch direct sampling)"
echo "  - Long wire antenna (5-10m) strongly recommended for HF reception"
echo "  - fldigi requires Xvfb on headless Raspberry Pi"
echo ""

# ── Step 6d: Signal classifier & SEI data directories ──
mkdir -p "$(dirname "$0")/ml/signal_classifier/data/collected"
mkdir -p "$(dirname "$0")/ml/signal_classifier/checkpoints"
mkdir -p "$(dirname "$0")/ml/signal_classifier/exports"
mkdir -p "$(dirname "$0")/ml/signal_classifier/reports"
mkdir -p "$(dirname "$0")/ml/sei/data/collected"
mkdir -p "$(dirname "$0")/ml/sei/checkpoints"
mkdir -p "$(dirname "$0")/ml/sei/exports"
mkdir -p "$(dirname "$0")/ml/sei/reports"
pass "Signal classifier & SEI directories created"

# ── Step 7: RTL-SDR Blog V4 driver ──
# MUST run after dump1090 install — dump1090-mutability pulls in stock librtlsdr0
# which does NOT support the V4 (R828D tuner) and causes "PLL not locked" errors.
# The Blog driver installs to /usr/local/lib and /usr/local/bin, overriding the
# stock /usr/lib binaries while keeping dump1090's apt dependency satisfied.
echo ""
echo "── Step 7: RTL-SDR Blog V4 driver ──"
if rtl_test -t 2>&1 | grep -q "Blog V4 Detected"; then
    pass "RTL-SDR Blog V4 driver already installed"
else
    echo "Building RTL-SDR Blog driver from source..."
    RTLSDR_BUILD_DIR="/tmp/rtl-sdr-blog-build"
    rm -rf "$RTLSDR_BUILD_DIR"
    git clone https://github.com/rtlsdrblog/rtl-sdr-blog.git "$RTLSDR_BUILD_DIR"
    cd "$RTLSDR_BUILD_DIR"
    mkdir build && cd build
    cmake ../ -DINSTALL_UDEV_RULES=ON
    make -j"$(nproc)"
    sudo make install
    sudo ldconfig
    sudo cp ../rtl-sdr.rules /etc/udev/rules.d/
    # Ensure the Blog library overrides the stock one in the system lib path
    # (stock librtlsdr from dump1090 dep lives in /lib/aarch64-linux-gnu/)
    if [ -d /lib/aarch64-linux-gnu ]; then
        sudo cp /usr/local/lib/librtlsdr.so.0* /lib/aarch64-linux-gnu/
        sudo ldconfig
    fi
    cd "$OLDPWD"
    rm -rf "$RTLSDR_BUILD_DIR"
    pass "RTL-SDR Blog V4 driver installed"
fi

# ── Step 7b: Test RTL-SDR ──
echo ""
echo "── Step 7b: RTL-SDR test ──"
if command -v rtl_test &>/dev/null; then
    # Stop dump1090 temporarily if running — it holds exclusive access to the SDR
    dump1090_was_running=false
    if systemctl is-active --quiet dump1090-mutability 2>/dev/null; then
        dump1090_was_running=true
        sudo systemctl stop dump1090-mutability
        sleep 1
    fi

    rtl_output=$(timeout --signal=KILL 5 rtl_test -t 2>&1 || true)
    if echo "$rtl_output" | grep -q "R828D"; then
        pass "RTL-SDR Blog V4 detected (R828D tuner)"
    elif echo "$rtl_output" | grep -q "usb_open\|usb_claim"; then
        warn "RTL-SDR device busy or permission denied — check USB access"
    else
        warn "RTL-SDR not detected — web stream mode will be used"
    fi

    # Restart dump1090 if we stopped it
    if $dump1090_was_running; then
        sudo systemctl start dump1090-mutability
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

# ── Step 9a: hailo_pci driver params (CRITICAL on 16KB-page kernels) ──
# Debian Trixie's Pi kernel uses 16KB pages; the driver then sets the DMA
# descriptor page size to 16384, exceeding the Hailo-8 HW max of 4096, so
# InferModel.configure() fails with HAILO_INTERNAL_FAILURE(8) and the app
# silently falls back to CPU. force_desc_page_size=4096 caps it to the HW max.
# force_allocation_from_driver=1 also removes a noisy kernel-6.18 find_vma WARNING.
HAILO_CONF="/etc/modprobe.d/hailo_pci.conf"
HAILO_OPTS="options hailo_pci force_desc_page_size=4096 force_allocation_from_driver=1"
if [ "$(cat "$HAILO_CONF" 2>/dev/null)" != "$HAILO_OPTS" ]; then
    echo "$HAILO_OPTS" | sudo tee "$HAILO_CONF" > /dev/null
    pass "hailo_pci params written to $HAILO_CONF"
    # Reload the module so params take effect now (needs the service released first)
    if lsmod | grep -q hailo_pci; then
        sudo systemctl stop hailort.service 2>/dev/null || true
        if sudo modprobe -r hailo_pci 2>/dev/null && sudo modprobe hailo_pci 2>/dev/null; then
            pass "hailo_pci reloaded with new params"
        else
            warn "Could not hot-reload hailo_pci — reboot to apply params"
        fi
        sudo systemctl start hailort.service 2>/dev/null || true
    fi
else
    pass "hailo_pci params already set"
fi
# Confirm the critical param actually took effect
DESC=$(cat /sys/module/hailo_pci/parameters/force_desc_page_size 2>/dev/null || echo "?")
[ "$DESC" = "4096" ] && pass "force_desc_page_size=4096 active" \
    || warn "force_desc_page_size=$DESC (want 4096) — reboot may be required"

if command -v hailortcli &>/dev/null; then
    if hailortcli fw-control identify 2>/dev/null || hailortcli fw-control identify 2>&1 | grep -q "Hailo-8"; then
        pass "Hailo NPU detected"

        # ── Step 9b: provide hailo_platform inside the venv ──
        # pyhailort is not pip-installable; it ships in the apt python3-hailort
        # package for the SYSTEM python. Symlink it into the venv, but only if the
        # venv python matches the system python (the .so is ABI-specific).
        SYS_PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        VENV_PYVER=$("$VENV_DIR/bin/python3" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        SITE_PKGS=$("$VENV_DIR/bin/python3" -c "import site; print(site.getsitepackages()[0])")
        HP_SRC="/usr/lib/python3/dist-packages/hailo_platform"
        if [ "$SYS_PYVER" != "$VENV_PYVER" ]; then
            warn "venv python $VENV_PYVER != system python $SYS_PYVER — hailo_platform .so may be incompatible"
        fi
        if [ ! -e "$SITE_PKGS/hailo_platform" ] && [ -d "$HP_SRC" ]; then
            ln -s "$HP_SRC" "$SITE_PKGS/hailo_platform"
            pass "hailo_platform symlinked into venv"
        fi
        # Verify it actually imports, and that numpy is 1.x (pyhailort ABI)
        if "$VENV_DIR/bin/python3" - <<'PYEOF'
import sys
try:
    import hailo_platform, numpy
except Exception as e:
    print("import error:", e); sys.exit(1)
major = int(numpy.__version__.split('.')[0])
if major != 1:
    print("numpy %s is not 1.x — pyhailort needs the numpy 1.x ABI" % numpy.__version__); sys.exit(2)
sys.exit(0)
PYEOF
        then
            pass "hailo_platform imports in venv (numpy 1.x OK)"
        else
            fail "hailo_platform not usable in venv — Hailo backend will be unavailable"
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

# ── Step 9c: Pre-cache HuggingFace models for offline operation ──
# The Hailo path needs the whisper-tiny tokenizer; the CPU fallback needs the
# faster-whisper tiny model. Pre-fetch both so the node runs air-gapped after
# first setup. Uses $HF_TOKEN + hf_transfer if available (faster, higher limits).
echo ""
echo "── Step 9c: Pre-cache Whisper models (offline) ──"
bash "$(dirname "$0")/scripts/download_models.sh" --hf-cache \
    && pass "Whisper tokenizer + faster-whisper model cached" \
    || warn "Model pre-cache incomplete — first transcription will need network"

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
