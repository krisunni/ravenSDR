# Mel spectrogram utilities for Hailo Whisper inference
# Pure-numpy implementation (no PyTorch dependency).
# Numerically matches the reference torch.stft-based mel to < 1e-5 (see
# scripts/debug.py mel parity check).

import os
from functools import lru_cache

import numpy as np

SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
CHUNK_LENGTH_S = 10  # Hailo encoder expects 10-second chunks
N_SAMPLES = CHUNK_LENGTH_S * SAMPLE_RATE  # 160,000 samples
N_FRAMES = N_SAMPLES // HOP_LENGTH  # 1000 frames

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models")


def pad_or_trim(array, length=N_SAMPLES, axis=-1):
    """Pad or trim a numpy audio array to exactly `length` samples."""
    if array.shape[axis] > length:
        array = array.take(indices=range(length), axis=axis)
    if array.shape[axis] < length:
        pad_widths = [(0, 0)] * array.ndim
        pad_widths[axis] = (0, length - array.shape[axis])
        array = np.pad(array, pad_widths)
    return array


@lru_cache(maxsize=None)
def mel_filters(n_mels=80):
    """Load pre-computed mel filterbank from mel_filters.npz -> (n_mels, N_FFT//2+1)."""
    filters_path = os.path.join(MODELS_DIR, "mel_filters.npz")
    with np.load(filters_path, allow_pickle=False) as f:
        return f[f"mel_{n_mels}"]


def log_mel_spectrogram(audio, n_mels=80):
    """
    Compute the log-mel spectrogram from an audio waveform, numpy-only.

    Mirrors the Whisper reference (torch.stft, center=True, reflect padding,
    periodic Hann window), returning a numpy array of shape (n_mels, N_FRAMES).

    Parameters
    ----------
    audio : np.ndarray
        Audio waveform at 16 kHz, float32.
    n_mels : int
        Number of mel bands (80 for whisper-tiny).

    Returns
    -------
    np.ndarray, shape (n_mels, n_frames)
    """
    audio = np.asarray(audio, dtype=np.float32)

    # center=True: reflect-pad by N_FFT//2 on both ends (matches torch.stft default)
    pad = N_FFT // 2
    padded = np.pad(audio, (pad, pad), mode="reflect")

    # Periodic Hann window (torch.hann_window default periodic=True uses N, not N-1)
    window = (0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT)).astype(np.float32)

    # Frame the signal at HOP_LENGTH and apply the window
    n_frames = 1 + (len(padded) - N_FFT) // HOP_LENGTH
    idx = np.arange(N_FFT)[None, :] + HOP_LENGTH * np.arange(n_frames)[:, None]
    frames = padded[idx] * window                       # (n_frames, N_FFT)

    spec = np.fft.rfft(frames, n=N_FFT, axis=1)          # (n_frames, N_FFT//2+1)
    spec = spec[:-1]                                     # drop last frame (torch [..., :-1])
    magnitudes = (np.abs(spec) ** 2).T                   # (N_FFT//2+1, n_frames-1)

    filters = mel_filters(n_mels)                        # (n_mels, N_FFT//2+1)
    mel_spec = filters @ magnitudes                      # (n_mels, n_frames-1)

    log_spec = np.log10(np.clip(mel_spec, 1e-10, None))
    log_spec = np.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.astype(np.float32)
