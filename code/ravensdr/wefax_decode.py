# Pure-numpy WEFAX / HF radiofax decoder: WAV -> PNG.
#
# No fldigi, no Xvfb, no audio loopback. WEFAX weather charts are transmitted as
# frequency-modulated audio: 1500 Hz = black, 2300 Hz = white (1900 Hz center),
# scanned line-by-line. We FM-demodulate the recorded audio, map instantaneous
# frequency to pixel brightness, slice it into lines at the transmit rate, and
# assemble a grayscale image. Includes optional deskew (LPM search) and left-edge
# alignment from the phasing signal.

import logging
import wave

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# --- WEFAX / marine radiofax standard ---
BLACK_FREQ = 1500.0        # Hz -> black pixel
WHITE_FREQ = 2300.0        # Hz -> white pixel
CENTER_FREQ = 1900.0       # Hz carrier
DEFAULT_LPM = 120.0        # lines per minute (weather charts)
DEFAULT_IOC = 576          # index of cooperation
IMAGE_WIDTH = 1809         # pixels per line (~ pi * IOC 576)


def _read_wav(path):
    """Read a mono/stereo 16-bit WAV -> (sample_rate, float64 samples)."""
    with wave.open(path, "rb") as w:
        fs = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return fs, data


def _analytic(sig):
    """Analytic (complex) signal via the FFT Hilbert method — numpy only, no scipy."""
    n = len(sig)
    spectrum = np.fft.fft(sig)
    h = np.zeros(n)
    if n % 2 == 0:
        h[0] = h[n // 2] = 1.0
        h[1:n // 2] = 2.0
    else:
        h[0] = 1.0
        h[1:(n + 1) // 2] = 2.0
    return np.fft.ifft(spectrum * h)


def _fm_demodulate(sig, fs):
    """Instantaneous frequency (Hz) of a bandpass FM signal."""
    analytic = _analytic(sig)
    phase = np.unwrap(np.angle(analytic))
    inst_freq = np.diff(phase) * fs / (2.0 * np.pi)
    return np.append(inst_freq, inst_freq[-1])


def _bandpass_fft(sig, fs, lo=900.0, hi=2900.0):
    """Crude brick-wall bandpass via FFT masking to reject out-of-band noise."""
    n = len(sig)
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    spec = np.fft.rfft(sig)
    spec[(freqs < lo) | (freqs > hi)] = 0.0
    return np.fft.irfft(spec, n=n)


def _freq_to_brightness(inst_freq):
    """Map FSK frequency to [0,1] brightness (0=black@1500Hz, 1=white@2300Hz)."""
    b = (inst_freq - BLACK_FREQ) / (WHITE_FREQ - BLACK_FREQ)
    return np.clip(b, 0.0, 1.0)


def _assemble(brightness, fs, lpm, width):
    """Slice the brightness stream into lines and resample each to `width` columns."""
    spl = fs * 60.0 / lpm                       # samples per line (fractional)
    nlines = int(len(brightness) // spl)
    if nlines < 1:
        return np.zeros((1, width))
    line_starts = (np.arange(nlines) * spl)[:, None]
    col = (np.arange(width) / width) * spl      # fractional sample offset within a line
    idx = np.clip((line_starts + col).astype(np.int64), 0, len(brightness) - 1)
    return brightness[idx]                       # (nlines, width)


def _deskew_score(brightness, fs, cand, width):
    """Correlation between adjacent lines — peaks at the true line rate."""
    img = _assemble(brightness, fs, cand, width)
    if img.shape[0] < 3:
        return -np.inf
    a = img[:-1] - img[:-1].mean(axis=1, keepdims=True)
    b = img[1:] - img[1:].mean(axis=1, keepdims=True)
    return float(np.mean(np.sum(a * b, axis=1)))


def _best_lpm(brightness, fs, lpm, width):
    """Deskew via two-stage LPM search (coarse 0.1 then fine 0.01).

    A residual LPM error of even ~0.01 shears the image tens of pixels top-to-
    bottom, so the fine stage matters — the coarse step alone leaves a visible slant.
    """
    def search(center, half, step):
        cands = np.arange(center - half, center + half + 1e-9, step)
        scores = [_deskew_score(brightness, fs, c, width) for c in cands]
        return float(cands[int(np.argmax(scores))])

    coarse = search(lpm, 0.6, 0.1)
    return search(coarse, 0.1, 0.01)


def _align_left_edge(img, phasing_rows=25):
    """Roll columns so the phasing black start-bar sits at the left edge.

    Only near-white rows are used, so the periodic grid/content of the chart
    body can't masquerade as the phasing bar. The phasing signal is a mostly-
    white line with one narrow black bar marking the left edge.
    """
    rows = min(phasing_rows, img.shape[0])
    if rows < 2:
        return img
    band = img[:rows]
    white_rows = band[band.mean(axis=1) > 0.85]     # keep phasing lines only
    if white_rows.shape[0] < 2:
        return img
    col_darkness = 1.0 - white_rows.mean(axis=0)    # peaks at the black start-bar
    # Use the leading edge of the darkest run as column 0
    shift = int(np.argmax(col_darkness))
    return np.roll(img, -shift, axis=1)


def decode_wav_to_png(wav_path, png_path, lpm=DEFAULT_LPM, width=IMAGE_WIDTH,
                      deskew=True, align=True, bandpass=True):
    """Decode a WEFAX audio recording to a grayscale PNG.

    Returns a dict of metadata (lines, lpm, width, mean_brightness) on success,
    or None if the audio was unusable.
    """
    fs, sig = _read_wav(wav_path)
    if len(sig) < fs:  # < 1 second is not a chart
        log.error("WEFAX decode: audio too short (%d samples @ %d Hz)", len(sig), fs)
        return None

    if bandpass:
        sig = _bandpass_fft(sig, fs)
    inst_freq = _fm_demodulate(sig, fs)
    brightness = _freq_to_brightness(inst_freq)

    use_lpm = _best_lpm(brightness, fs, lpm, width) if deskew else lpm
    img = _assemble(brightness, fs, use_lpm, width)
    if align:
        img = _align_left_edge(img)

    # Normalize contrast (stretch 2nd..98th percentile) for readable charts
    lo, hi = np.percentile(img, 2), np.percentile(img, 98)
    if hi > lo:
        img = np.clip((img - lo) / (hi - lo), 0.0, 1.0)

    pixels = (img * 255.0).astype(np.uint8)
    Image.fromarray(pixels, mode="L").save(png_path)
    meta = {"lines": int(img.shape[0]), "lpm": round(float(use_lpm), 2),
            "width": width, "mean_brightness": round(float(img.mean()), 3)}
    log.info("WEFAX decoded %s: %d lines, lpm=%.2f", png_path, meta["lines"], meta["lpm"])
    return meta


# ---------------------------------------------------------------------------
# Encoder — used only for tests / demos: image -> WEFAX WAV. Lets us prove the
# decoder round-trips without needing a live HF antenna or an external sample.
# ---------------------------------------------------------------------------
def encode_image_to_wav(img_gray, wav_path, fs=11025, lpm=DEFAULT_LPM,
                        width=IMAGE_WIDTH, phasing_lines=20, amplitude=0.7):
    """Modulate a grayscale image (uint8 HxW) into a WEFAX FSK WAV."""
    img = np.asarray(img_gray, dtype=np.float64) / 255.0
    if img.shape[1] != width:
        cols = np.clip((np.arange(width) / width * img.shape[1]).astype(int), 0, img.shape[1] - 1)
        img = img[:, cols]
    spl = int(round(fs * 60.0 / lpm))

    # Phasing signal: narrow black start-bar (~4.5% of line) then white
    phas = np.ones(width); phas[: int(0.045 * width)] = 0.0
    lines = [phas] * phasing_lines + [img[i] for i in range(img.shape[0])]

    brightness = np.concatenate([np.interp(np.arange(spl) / spl * width,
                                            np.arange(width), ln) for ln in lines])
    inst_freq = BLACK_FREQ + brightness * (WHITE_FREQ - BLACK_FREQ)
    phase = np.cumsum(2.0 * np.pi * inst_freq / fs)
    samples = (amplitude * np.sin(phase) * 32767).astype(np.int16)

    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(fs)
        w.writeframes(samples.tobytes())
    return {"lines": len(lines), "samples": len(samples), "fs": fs}
