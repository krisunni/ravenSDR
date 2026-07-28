#!/usr/bin/env python3
"""RadioML dataset loader and custom class generator for signal classification.

Loads RadioML 2018.01A (DeepSig) HDF5 dataset, converts IQ samples to
spectrograms, and integrates custom classes (ADS-B, NOAA APT, WEFAX).

Training pipeline runs on x86, NOT on Raspberry Pi.
"""

import json
import os
import sys

import numpy as np

# Spectrogram parameters (match signal_classifier.py)
FFT_SIZE = 256

# Window length for a custom capture, in IQ samples.
#
# MUST match what the runtime classifies. signal_classifier.classify_iq is fed
# ~24000-sample chunks, which make a (186, 256) spectrogram; truncating training
# samples to 1024 makes a (7, 256) one that is then stretched to the same 224
# rows. Measured on one capture: mean pixel difference 30/255 and correlation
# only 0.49 — the model would be trained on a visibly different distribution
# from the one it sees in production, which costs accuracy no matter how well
# training itself goes.
#
# RadioML records are natively 1024 samples, so mixing that corpus in requires
# setting this to 1024 and accepting the shorter runtime window to match.
RUNTIME_SAMPLE_LEN = 24000
FFT_HOP = FFT_SIZE // 2
SPECTROGRAM_SIZE = 224

# Target modulation classes for ravenSDR
TARGET_CLASSES = [
    "AM", "FM", "WFM", "SSB", "P25", "DMR",
    "ADSB", "NOAA_APT", "WEFAX", "CW",
    # Digital modulations the node can actually label from its presets. Without
    # these, collection wrote AFSK1200/FSK/MSK/OOK directories that the dataset
    # builder silently ignored — 1162 of 2271 samples dropped with no warning.
    "OOK", "FSK", "MSK", "AFSK1200",
    "unknown",
]

# RadioML 2018.01A class name mapping to ravenSDR classes
RADIOML_CLASS_MAP = {
    "OOK": "AM",
    "4ASK": "AM",
    "8ASK": "AM",
    "BPSK": "unknown",
    "QPSK": "unknown",
    "8PSK": "unknown",
    "16QAM": "unknown",
    "32QAM": "unknown",
    "64QAM": "unknown",
    "128QAM": "unknown",
    "256QAM": "unknown",
    "AM-SSB-WC": "SSB",
    "AM-SSB-SC": "SSB",
    "AM-DSB-WC": "AM",
    "AM-DSB-SC": "AM",
    "FM": "FM",
    "GMSK": "FM",
    "OQPSK": "unknown",
    "16APSK": "unknown",
    "32APSK": "unknown",
    "64APSK": "unknown",
    "128APSK": "unknown",
}


def iq_to_spectrogram(iq_samples, fft_size=FFT_SIZE, hop=FFT_HOP):
    """Convert complex IQ samples to power spectrogram."""
    window = np.hanning(fft_size)
    n_frames = max(1, (len(iq_samples) - fft_size) // hop + 1)
    spectrogram = np.zeros((n_frames, fft_size), dtype=np.float64)

    for i in range(n_frames):
        start = i * hop
        frame = iq_samples[start:start + fft_size]
        if len(frame) < fft_size:
            frame = np.pad(frame, (0, fft_size - len(frame)))
        windowed = frame * window
        fft_result = np.fft.fftshift(np.fft.fft(windowed))
        power = np.abs(fft_result) ** 2
        power = np.maximum(power, 1e-20)
        spectrogram[i] = 10 * np.log10(power)

    return spectrogram


def spectrogram_to_image(spectrogram, size=SPECTROGRAM_SIZE):
    """Normalize and resize spectrogram to uint8 image."""
    smin = spectrogram.min()
    smax = spectrogram.max()
    if smax - smin < 1e-6:
        normalized = np.zeros_like(spectrogram, dtype=np.float64)
    else:
        normalized = (spectrogram - smin) / (smax - smin) * 255.0

    img = normalized.astype(np.uint8)

    h, w = img.shape
    if h == size and w == size:
        return img

    row_idx = np.clip((np.arange(size) * h / size).astype(int), 0, h - 1)
    col_idx = np.clip((np.arange(size) * w / size).astype(int), 0, w - 1)
    return img[np.ix_(row_idx, col_idx)]


def augment_iq(iq_samples, rng=None):
    """Apply data augmentation to IQ samples.

    - Random frequency shift (±5% of bandwidth)
    - SNR variation (additive Gaussian noise)
    - Random phase rotation (0-2π)
    """
    if rng is None:
        rng = np.random.default_rng()

    # Phase rotation
    phase = rng.uniform(0, 2 * np.pi)
    iq_samples = iq_samples * np.exp(1j * phase)

    # Frequency shift
    n = len(iq_samples)
    shift = rng.uniform(-0.05, 0.05)
    t = np.arange(n) / n
    iq_samples = iq_samples * np.exp(2j * np.pi * shift * t)

    # SNR variation (add noise)
    noise_level = rng.uniform(0.01, 0.3)
    noise = noise_level * (rng.standard_normal(n) + 1j * rng.standard_normal(n)) / np.sqrt(2)
    iq_samples = iq_samples + noise

    return iq_samples


def load_radioml(dataset_path):
    """Load RadioML 2018.01A HDF5 dataset.

    Args:
        dataset_path: path to GOLD_XYZ_OSC.0001_1024.hdf5

    Returns:
        (iq_samples, labels, snrs) numpy arrays
    """
    try:
        import h5py
    except ImportError:
        print("ERROR: h5py required — pip install h5py")
        sys.exit(1)

    print(f"Loading RadioML dataset from {dataset_path}...")
    with h5py.File(dataset_path, "r") as f:
        X = f["X"][:]  # (N, 1024, 2) — I/Q as two channels
        Y = f["Y"][:]  # (N, 24) — one-hot labels
        Z = f["Z"][:]  # (N,) — SNR values

    # Convert to complex
    iq = X[:, :, 0] + 1j * X[:, :, 1]

    # Convert one-hot to class indices
    labels = np.argmax(Y, axis=1)

    print(f"Loaded {len(iq)} samples, {Y.shape[1]} classes")
    return iq, labels, Z


def sample_frequency_hz(path):
    """Parse the capture frequency out of a collected filename.

    Collected as <UTCstamp>_<frequency_hz>.npy by
    signal_classifier.collect_sample.
    """
    try:
        return int(os.path.basename(path).rsplit("_", 1)[-1].split(".")[0])
    except (ValueError, IndexError):
        return 0


def load_custom_samples(data_dir, class_name, sample_len=RUNTIME_SAMPLE_LEN,
                        with_freqs=False):
    """Load custom IQ samples from .npy files in a directory.

    Args:
        data_dir: directory containing .npy IQ sample files
        class_name: class label to assign

    Returns:
        list of complex numpy arrays
    """
    samples = []
    freqs = []
    if not os.path.isdir(data_dir):
        return (samples, freqs) if with_freqs else samples

    for fname in os.listdir(data_dir):
        if fname.endswith(".npy"):
            path = os.path.join(data_dir, fname)
            try:
                iq = np.load(path)
                if np.iscomplexobj(iq) and len(iq) >= FFT_SIZE:
                    samples.append(iq[:sample_len])
                    freqs.append(sample_frequency_hz(path))
            except Exception:
                pass

    return (samples, freqs) if with_freqs else samples


def build_dataset(radioml_path=None, custom_dirs=None, augment=True, seed=42,
                  sample_len=RUNTIME_SAMPLE_LEN):
    """Build combined dataset of spectrograms and labels.

    Args:
        radioml_path: path to RadioML HDF5 file (optional)
        custom_dirs: dict of {class_name: directory_path} for custom classes
        augment: whether to apply augmentation
        seed: random seed

    Returns:
        (images, labels, class_names) — images as (N, 224, 224) uint8,
        labels as (N,) int, class_names as list
    """
    rng = np.random.default_rng(seed)
    images = []
    labels = []
    class_names = TARGET_CLASSES[:]

    # Load RadioML
    if radioml_path and os.path.exists(radioml_path):
        iq_data, radioml_labels, snrs = load_radioml(radioml_path)

        # Map RadioML classes to ravenSDR classes
        # Get RadioML class names from the dataset
        radioml_class_names = [
            "OOK", "4ASK", "8ASK", "BPSK", "QPSK", "8PSK",
            "16QAM", "32QAM", "64QAM", "128QAM", "256QAM",
            "AM-SSB-WC", "AM-SSB-SC", "AM-DSB-WC", "AM-DSB-SC",
            "FM", "GMSK", "OQPSK",
            "16APSK", "32APSK", "64APSK", "128APSK",
        ]

        for i in range(len(iq_data)):
            rm_idx = radioml_labels[i]
            if rm_idx >= len(radioml_class_names):
                continue

            rm_class = radioml_class_names[rm_idx]
            target_class = RADIOML_CLASS_MAP.get(rm_class, "unknown")

            if target_class not in class_names:
                continue

            iq = iq_data[i]
            if augment and rng.random() > 0.5:
                iq = augment_iq(iq, rng)

            spec = iq_to_spectrogram(iq)
            img = spectrogram_to_image(spec)
            images.append(img)
            labels.append(class_names.index(target_class))

        print(f"Processed {len(images)} RadioML samples")

    # Load custom classes
    if custom_dirs:
        for class_name, data_dir in custom_dirs.items():
            if class_name not in class_names:
                continue

            samples = load_custom_samples(data_dir, class_name, sample_len)
            class_idx = class_names.index(class_name)

            for iq in samples:
                if augment and rng.random() > 0.5:
                    iq = augment_iq(iq, rng)

                spec = iq_to_spectrogram(iq)
                img = spectrogram_to_image(spec)
                images.append(img)
                labels.append(class_idx)

            print(f"Loaded {len(samples)} custom {class_name} samples")

    if not images:
        print("WARNING: No data loaded. Provide RadioML dataset or custom samples.")
        return np.array([]), np.array([]), class_names

    images = np.array(images, dtype=np.uint8)
    labels = np.array(labels, dtype=np.int64)

    # Save class mapping
    class_map = {str(i): name for i, name in enumerate(class_names)}
    return images, labels, class_names


def split_dataset(images, labels, train_ratio=0.7, val_ratio=0.15, seed=42):
    """Stratified train/val/test split.

    Returns:
        (train_images, train_labels, val_images, val_labels, test_images, test_labels)
    """
    rng = np.random.default_rng(seed)
    n = len(images)
    indices = np.arange(n)
    rng.shuffle(indices)

    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)

    train_idx = indices[:n_train]
    val_idx = indices[n_train:n_train + n_val]
    test_idx = indices[n_train + n_val:]

    return (
        images[train_idx], labels[train_idx],
        images[val_idx], labels[val_idx],
        images[test_idx], labels[test_idx],
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build signal classification dataset")
    parser.add_argument("--radioml", type=str, help="Path to RadioML HDF5 file")
    parser.add_argument("--custom-dir", type=str, help="Directory with custom class subdirs")
    parser.add_argument("--output", type=str, default="data/dataset.npz", help="Output path")
    parser.add_argument("--sample-len", type=int, default=RUNTIME_SAMPLE_LEN,
                        help="IQ samples per training window. Must match the "
                             "runtime window (%d) or the model is trained on a "
                             "different distribution than it infers on. Use 1024 "
                             "only when mixing in RadioML." % RUNTIME_SAMPLE_LEN)
    args = parser.parse_args()

    custom_dirs = {}
    if args.custom_dir:
        for class_name in TARGET_CLASSES:
            d = os.path.join(args.custom_dir, class_name)
            if os.path.isdir(d):
                custom_dirs[class_name] = d

        # A directory of real, labelled captures that is not in TARGET_CLASSES
        # is a taxonomy bug, not an empty class — say so instead of dropping it.
        if os.path.isdir(args.custom_dir):
            unknown = [n for n in sorted(os.listdir(args.custom_dir))
                       if os.path.isdir(os.path.join(args.custom_dir, n))
                       and n not in TARGET_CLASSES]
            for n in unknown:
                count = len([f for f in os.listdir(os.path.join(args.custom_dir, n))
                             if f.endswith(".npy")])
                print("WARNING: ignoring %d samples in '%s' — not in "
                      "TARGET_CLASSES" % (count, n))

    if args.radioml and args.sample_len != 1024:
        print("WARNING: RadioML records are 1024 samples but --sample-len is %d."
              % args.sample_len)
        print("         Mixing the two trains on inconsistent window lengths.")

    images, labels, class_names = build_dataset(
        radioml_path=args.radioml,
        custom_dirs=custom_dirs if custom_dirs else None,
        sample_len=args.sample_len,
    )

    if len(images) > 0:
        train_imgs, train_lbls, val_imgs, val_lbls, test_imgs, test_lbls = \
            split_dataset(images, labels)

        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        np.savez_compressed(
            args.output,
            train_images=train_imgs, train_labels=train_lbls,
            val_images=val_imgs, val_labels=val_lbls,
            test_images=test_imgs, test_labels=test_lbls,
        )

        # Save class mapping
        class_map = {str(i): name for i, name in enumerate(class_names)}
        map_path = os.path.splitext(args.output)[0] + "_classes.json"
        with open(map_path, "w") as f:
            json.dump(class_map, f, indent=2)

        print(f"Dataset saved: {args.output}")
        print(f"  Train: {len(train_imgs)}, Val: {len(val_imgs)}, Test: {len(test_imgs)}")
        print(f"  Class mapping: {map_path}")
