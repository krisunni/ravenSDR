# Signal classifier — IQ to spectrogram CNN on Hailo-8L NPU
#
# Converts raw IQ samples to spectrogram images and classifies modulation type
# using a MobileNetV2 CNN. Runs on Hailo-8L NPU with CPU fallback.

import datetime
import json
import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

# Classification target classes
MODULATION_CLASSES = [
    "AM", "FM", "WFM", "SSB", "P25", "DMR",
    "ADSB", "NOAA_APT", "WEFAX", "CW", "unknown",
]

CLASS_INDEX = {name: i for i, name in enumerate(MODULATION_CLASSES)}

# Inference parameters
CONFIDENCE_THRESHOLD = 0.7      # minimum top-class confidence to emit
UNCERTAINTY_MARGIN = 0.10       # top two within this = uncertain
SPECTROGRAM_SIZE = 224           # MobileNetV2 input size
FFT_SIZE = 256                   # FFT window size
FFT_HOP = FFT_SIZE // 2         # 50% overlap
CHUNK_SAMPLES = 240000           # 100ms at 2.4 MHz sample rate

# Self-supervised data collection
COLLECTED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "ml", "signal_classifier", "data", "collected"
)

# Corpus collection, labelled from the PRESET rather than from the classifier.
#
# The original trigger only saved a sample when the classifier's own output
# already matched the preset — which is circular: a trained classifier is needed
# to collect the data required to train one. Worse, with no HEF the CPU fallback
# can only ever emit WFM/FM/CW/AM/SSB, so ADSB, WEFAX, NOAA_APT, P25 and DMR
# could never be collected at all. Result after months of running: one file.
#
# The operator's tuning choice IS ground truth. Tuned to a preset declaring
# expected_modulation, every detected transmission on that channel is a labelled
# sample regardless of what the untrained classifier believes. That breaks the
# deadlock and needs no model.
COLLECT_MIN_SNR_DB = 10.0        # below this it may be noise, not a transmission
COLLECT_MAX_PER_CLASS = 5000     # bound the corpus; the SD card is not infinite
COLLECT_MIN_INTERVAL_S = 2.0     # per class — diversity beats 10 copies a second


def iq_to_spectrogram(iq_samples, fft_size=FFT_SIZE, hop=FFT_HOP):
    """Convert complex IQ samples to a power spectrogram in dBm.

    Args:
        iq_samples: numpy array of complex64/complex128 IQ samples
        fft_size: FFT window size
        hop: hop size between windows

    Returns:
        2D numpy array (time_bins, freq_bins) in dBm
    """
    # Apply Hann window
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
        # Convert to dBm (avoid log of zero)
        power = np.maximum(power, 1e-20)
        spectrogram[i] = 10 * np.log10(power)

    return spectrogram


def spectrogram_to_image(spectrogram, size=SPECTROGRAM_SIZE):
    """Normalize spectrogram to 0-255 uint8 and resize to model input size.

    Args:
        spectrogram: 2D numpy array in dBm
        size: target image size (square)

    Returns:
        numpy array of shape (size, size) uint8
    """
    # Min-max normalize to 0-255
    smin = spectrogram.min()
    smax = spectrogram.max()
    if smax - smin < 1e-6:
        normalized = np.zeros_like(spectrogram, dtype=np.float64)
    else:
        normalized = (spectrogram - smin) / (smax - smin) * 255.0

    img = normalized.astype(np.uint8)

    # Resize to target size using nearest-neighbor (fast, no scipy dependency)
    h, w = img.shape
    if h == size and w == size:
        return img

    row_idx = (np.arange(size) * h / size).astype(int)
    col_idx = (np.arange(size) * w / size).astype(int)
    row_idx = np.clip(row_idx, 0, h - 1)
    col_idx = np.clip(col_idx, 0, w - 1)
    resized = img[np.ix_(row_idx, col_idx)]
    return resized


class SignalClassifier:
    """Classifies RF signal modulation type from IQ samples using CNN on Hailo-8L."""

    def __init__(self, emit_fn=None, hef_path=None, class_map_path=None):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self.hef_path = hef_path
        self._classes = MODULATION_CLASSES[:]
        self._backend = "none"
        self._collect_last_ts = {}      # label -> last save time (rate limit)
        self._collect_counts = {}       # label -> files on disk (cap)
        self._collected_total = 0
        self._collect_skipped_snr = 0
        # Ground-truth label for collection, set from the tuned preset's
        # expected_modulation. None disables collection entirely.
        self.collect_label = None
        self._model = None
        self._vdevice = None
        self._running = False
        self._lock = threading.Lock()
        self.confidence_threshold = CONFIDENCE_THRESHOLD  # runtime-tunable

        # SEI pipeline (set via set_sei_model)
        self._sei_model = None

        # Stats
        self._classifications_total = 0
        self._correct_vs_preset = 0
        self._compared_vs_preset = 0

        # Load class map if provided
        if class_map_path and os.path.exists(class_map_path):
            with open(class_map_path) as f:
                mapping = json.load(f)
                self._classes = [mapping[str(i)] for i in range(len(mapping))]

        # Try to load Hailo model
        if hef_path and os.path.exists(hef_path):
            self._init_hailo(hef_path)
        else:
            log.info("Signal classifier: no HEF model, using CPU fallback")
            self._backend = "cpu"

    def _init_hailo(self, hef_path):
        """Initialize Hailo-8L inference model."""
        try:
            from hailo_platform import HEF, VDevice
            hef = HEF(hef_path)
            self._vdevice = VDevice()
            self._model = self._vdevice.create_infer_model(hef)
            self._backend = "hailo"
            log.info("Signal classifier loaded on Hailo-8L: %s", hef_path)
        except Exception as e:
            log.warning("Signal classifier: Hailo init failed (%s), using CPU fallback", e)
            self._backend = "cpu"

    def set_sei_model(self, sei_model):
        """Set SEI model for emitter fingerprinting after classification."""
        self._sei_model = sei_model

    def apply_settings(self, settings):
        """Apply runtime settings from the Settings tab (see config.py)."""
        try:
            self.confidence_threshold = float(settings.get(
                "classifier_confidence", self.confidence_threshold))
        except (TypeError, ValueError):
            pass

    @property
    def backend(self):
        return self._backend

    @property
    def is_active(self):
        return self._backend != "none"

    def classify_iq(self, iq_samples, frequency_hz=0, expected_modulation=None):
        """Classify modulation type from raw IQ samples.

        Args:
            iq_samples: complex numpy array of IQ samples
            frequency_hz: center frequency for logging
            expected_modulation: ground truth from preset (for accuracy tracking)

        Returns:
            dict with classification result, or None if below confidence threshold
        """
        if len(iq_samples) < FFT_SIZE:
            return None

        # IQ -> spectrogram -> image
        spectrogram = iq_to_spectrogram(iq_samples)
        img = spectrogram_to_image(spectrogram)

        # Run inference
        if self._backend == "hailo" and self._model is not None:
            result = self._infer_hailo(img)
        else:
            result = self._infer_cpu(spectrogram)

        if result is None:
            return None

        modulation, confidence, probs = result

        # Check confidence threshold
        if confidence < self.confidence_threshold:
            return None

        # Check uncertainty (top two classes close)
        sorted_probs = sorted(probs, reverse=True)
        uncertain = len(sorted_probs) >= 2 and (sorted_probs[0] - sorted_probs[1]) < UNCERTAINTY_MARGIN

        self._classifications_total += 1

        # Accuracy tracking against preset ground truth
        if expected_modulation and expected_modulation != "unknown":
            self._compared_vs_preset += 1
            if modulation == expected_modulation:
                self._correct_vs_preset += 1
                # Self-supervised: save confirmed IQ chunk
                self._save_collected_sample(iq_samples, modulation, frequency_hz)

        utcnow = datetime.datetime.now(datetime.timezone.utc)
        now = utcnow.strftime("%Y-%m-%dT%H:%M:%S.") + \
              f"{utcnow.microsecond // 1000:03d}Z"

        classification = {
            "modulation": modulation,
            "confidence": round(confidence, 3),
            "frequency_hz": frequency_hz,
            "timestamp": now,
            "uncertain": bool(uncertain),
        }

        # Emit Socket.IO event
        self.emit_fn("signal_classified", classification)

        # Forward to SEI pipeline for emitter fingerprinting
        self._forward_to_sei(iq_samples, frequency_hz, modulation, confidence)

        return classification


    def collect_sample(self, iq_samples, label, frequency_hz, snr_db=None):
        """Save a labelled IQ sample, using the preset as ground truth.

        Returns the path written, or None if the sample was declined. Never
        raises — collection must not be able to break reception.
        """
        if not label or label == "unknown":
            return None
        if snr_db is not None and snr_db < COLLECT_MIN_SNR_DB:
            self._collect_skipped_snr += 1
            return None

        now = time.time()
        last = self._collect_last_ts.get(label, 0.0)
        if now - last < COLLECT_MIN_INTERVAL_S:
            return None

        save_dir = os.path.join(COLLECTED_DIR, label)
        try:
            os.makedirs(save_dir, exist_ok=True)
            if self._collect_counts.get(label) is None:
                self._collect_counts[label] = len(
                    [f for f in os.listdir(save_dir) if f.endswith(".npy")])
            if self._collect_counts[label] >= COLLECT_MAX_PER_CLASS:
                return None

            ts = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y%m%d_%H%M%S_%f")
            path = os.path.join(save_dir, f"{ts}_{frequency_hz}.npy")
            np.save(path, iq_samples)
        except OSError as e:
            log.warning("Could not write training sample: %s", e)
            return None

        self._collect_last_ts[label] = now
        self._collect_counts[label] += 1
        self._collected_total += 1
        return path

    def collection_stats(self):
        """Per-class corpus counts, read from disk so restarts stay accurate."""
        per_class = {}
        total = 0
        if os.path.isdir(COLLECTED_DIR):
            for name in sorted(os.listdir(COLLECTED_DIR)):
                sub = os.path.join(COLLECTED_DIR, name)
                if not os.path.isdir(sub):
                    continue
                n = len([f for f in os.listdir(sub) if f.endswith(".npy")])
                if n:
                    per_class[name] = n
                    total += n
        return {
            "dir": os.path.normpath(COLLECTED_DIR),
            "total": total,
            "per_class": per_class,
            "collected_this_run": self._collected_total,
            "skipped_low_snr": self._collect_skipped_snr,
            "min_snr_db": COLLECT_MIN_SNR_DB,
            "max_per_class": COLLECT_MAX_PER_CLASS,
        }

    def classify_segment(self, segment):
        """Classify a transmission segment from IQSegmenter and forward to SEI.

        Args:
            segment: iq_segmenter.Segment object

        Returns:
            dict with classification result, or None
        """
        result = self.classify_iq(
            segment.iq_samples,
            frequency_hz=segment.frequency_hz,
            expected_modulation=self.collect_label,
        )

        # A segment is a DETECTED transmission with a measured SNR — the best
        # training sample available. Label it from the preset, not the model.
        if self.collect_label:
            self.collect_sample(segment.iq_samples, self.collect_label,
                                segment.frequency_hz, snr_db=segment.snr_db)

        # Forward to SEI with full segment metadata
        if result is not None and self._sei_model is not None:
            try:
                self._sei_model.identify(
                    segment.iq_samples,
                    frequency_hz=segment.frequency_hz,
                    modulation=result["modulation"],
                    snr_db=segment.snr_db,
                    duration_ms=segment.duration_ms,
                )
            except Exception as e:
                log.debug("SEI segment forwarding error: %s", e)

        return result

    def _forward_to_sei(self, iq_samples, frequency_hz, modulation, confidence):
        """Forward classified IQ to SEI if conditions met."""
        if self._sei_model is None:
            return
        if confidence < CONFIDENCE_THRESHOLD:
            return
        try:
            self._sei_model.identify(
                iq_samples,
                frequency_hz=frequency_hz,
                modulation=modulation,
            )
        except Exception as e:
            log.debug("SEI forwarding error: %s", e)

    def _infer_hailo(self, img):
        """Run inference on Hailo-8L NPU.

        Args:
            img: 224x224 uint8 numpy array

        Returns:
            (modulation_name, confidence, probabilities) or None
        """
        try:
            # Expand to 3 channels (ImageNet-pretrained model expects RGB)
            input_data = np.stack([img, img, img], axis=-1).astype(np.uint8)
            input_data = np.expand_dims(input_data, axis=0)  # batch dim

            with self._lock:
                output = self._model.infer(input_data)

            # Output is softmax probabilities
            probs = output[0].flatten()
            if len(probs) < len(self._classes):
                return None

            probs = probs[:len(self._classes)]
            idx = int(np.argmax(probs))
            confidence = float(probs[idx])
            modulation = self._classes[idx]
            return modulation, confidence, probs.tolist()
        except Exception as e:
            log.warning("Hailo inference failed: %s", e)
            return self._infer_cpu(None)

    def _infer_cpu(self, spectrogram):
        """CPU fallback classifier using spectral feature heuristics.

        Simple rule-based classifier that analyzes spectrogram characteristics:
        - Bandwidth (narrow vs wide)
        - Peak-to-average ratio
        - Spectral symmetry
        """
        if spectrogram is None:
            return "unknown", 0.5, [0.5 / len(self._classes)] * len(self._classes)

        # Spectral features from mean power across time
        mean_spectrum = np.mean(spectrogram, axis=0)
        peak = np.max(mean_spectrum)
        avg = np.mean(mean_spectrum)
        std = np.std(mean_spectrum)

        # Bandwidth estimation: count bins above threshold
        threshold = avg + std
        occupied_bins = np.sum(mean_spectrum > threshold)
        bandwidth_ratio = occupied_bins / len(mean_spectrum)

        # Peak-to-average ratio
        par = peak - avg if avg != 0 else 0

        # Simple heuristic classification
        if bandwidth_ratio > 0.6:
            modulation = "WFM"
            confidence = min(0.85, 0.6 + bandwidth_ratio * 0.3)
        elif bandwidth_ratio > 0.3:
            modulation = "FM"
            confidence = min(0.82, 0.55 + bandwidth_ratio * 0.3)
        elif par > 20:
            modulation = "CW"
            confidence = min(0.80, 0.5 + par * 0.01)
        elif bandwidth_ratio < 0.05:
            modulation = "CW"
            confidence = 0.6
        elif bandwidth_ratio < 0.15:
            modulation = "AM"
            confidence = 0.65
        elif bandwidth_ratio < 0.25:
            modulation = "SSB"
            confidence = 0.6
        else:
            modulation = "unknown"
            confidence = 0.5

        # Build fake probability vector
        probs = [0.01] * len(self._classes)
        if modulation in CLASS_INDEX:
            probs[CLASS_INDEX[modulation]] = confidence
        # Normalize
        total = sum(probs)
        probs = [p / total for p in probs]

        return modulation, confidence, probs

    def _save_collected_sample(self, iq_samples, modulation, frequency_hz):
        """Save confirmed IQ chunk for self-supervised retraining."""
        try:
            save_dir = os.path.join(COLLECTED_DIR, modulation)
            os.makedirs(save_dir, exist_ok=True)
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            filename = f"{ts}_{frequency_hz}.npy"
            np.save(os.path.join(save_dir, filename), iq_samples)
        except Exception as e:
            log.debug("Failed to save collected sample: %s", e)

    def get_status(self):
        """Return classifier status dict for API."""
        accuracy = 0.0
        if self._compared_vs_preset > 0:
            accuracy = round(self._correct_vs_preset / self._compared_vs_preset, 3)

        return {
            "active": self.is_active,
            "model": "mobilenetv2",
            "backend": self._backend,
            "classifications_total": self._classifications_total,
            "accuracy_vs_presets": accuracy,
            "compared_count": self._compared_vs_preset,
            "correct_count": self._correct_vs_preset,
        }

    def stop(self):
        """Clean up resources."""
        self._model = None
        if self._vdevice:
            try:
                self._vdevice = None
            except Exception:
                pass
        log.info("Signal classifier stopped")
