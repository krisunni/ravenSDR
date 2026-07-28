# Specific Emitter Identification (SEI) — Hailo inference, cosine similarity,
# emitter database manager
#
# Processes raw IQ samples through a 1D CNN to produce 128-dimensional embedding
# vectors (fingerprints). Matches against a local database of known emitter
# fingerprints via cosine similarity. New emitters enrolled automatically.

import datetime
import json
import logging
import os
import tempfile
import threading

import numpy as np

log = logging.getLogger(__name__)

# SEI parameters
EMBEDDING_DIM = 128
HAILO_TIMEOUT_MS = 10000
MATCH_THRESHOLD = 0.85          # cosine similarity threshold for match
IQ_WINDOW_SIZE = 1024           # IQ samples per inference window
EMA_ALPHA = 0.1                 # exponential moving average for centroid updates
MIN_SNR_DB = 15.0               # minimum SNR for reliable fingerprinting
MIN_DURATION_MS = 100           # minimum transmission duration for SEI

# Database file
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_FILE = os.path.join(DATA_DIR, "emitter_db.json")


def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    dot = np.dot(a, b)
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def l2_normalize(v):
    """L2-normalize a vector."""
    norm = np.linalg.norm(v)
    if norm > 0:
        return v / norm
    return v


class EmitterRecord:
    """A known emitter in the database."""

    __slots__ = ("emitter_id", "label", "first_seen", "last_seen",
                 "observation_count", "frequency_history", "embedding_centroid")

    def __init__(self, emitter_id, label=None, first_seen=None, last_seen=None,
                 observation_count=0, frequency_history=None, embedding_centroid=None):
        self.emitter_id = emitter_id
        self.label = label
        self.first_seen = first_seen or datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"
        self.last_seen = last_seen or self.first_seen
        self.observation_count = observation_count
        self.frequency_history = frequency_history or []
        self.embedding_centroid = embedding_centroid  # numpy array or None

    def to_dict(self):
        d = {
            "emitter_id": self.emitter_id,
            "label": self.label,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "observation_count": self.observation_count,
            "frequency_history": self.frequency_history,
        }
        if self.embedding_centroid is not None:
            d["embedding_centroid"] = self.embedding_centroid.tolist()
        return d

    @classmethod
    def from_dict(cls, d):
        centroid = None
        if d.get("embedding_centroid"):
            centroid = np.array(d["embedding_centroid"], dtype=np.float32)
        return cls(
            emitter_id=d["emitter_id"],
            label=d.get("label"),
            first_seen=d.get("first_seen"),
            last_seen=d.get("last_seen"),
            observation_count=d.get("observation_count", 0),
            frequency_history=d.get("frequency_history", []),
            embedding_centroid=centroid,
        )


class SEIModel:
    """Specific Emitter Identification model and emitter database manager.

    Processes raw IQ samples through a 1D CNN on Hailo-8L to produce
    128-dimensional embedding vectors. Matches embeddings against known
    emitter database using cosine similarity.
    """

    def __init__(self, emit_fn=None, hef_path=None, db_path=None):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self.hef_path = hef_path
        self.db_path = db_path or DB_FILE
        self._backend = "none"
        self._model = None
        self._vdevice = None
        self._configured = None
        self._bindings = None
        self._input_shape = ()
        self._lock = threading.Lock()
        self.match_threshold = MATCH_THRESHOLD  # runtime-tunable via apply_settings

        # Emitter database
        self._emitters = {}  # emitter_id -> EmitterRecord
        self._next_id = 1
        self._db_lock = threading.Lock()

        # Load database
        self._load_db()

        # Try to load Hailo model
        if hef_path and os.path.exists(hef_path):
            self._init_hailo(hef_path)
        else:
            log.info("SEI model: no HEF model, using CPU fallback (random embeddings)")
            self._backend = "cpu"

    def _init_hailo(self, hef_path):
        """Initialize Hailo-8L inference model for SEI."""
        try:
            from hailo_platform import (HEF, VDevice, FormatType,
                                        HailoSchedulingAlgorithm)
            hef = HEF(hef_path)
            # ROUND_ROBIN so this can coexist with Whisper and the classifier;
            # a bare VDevice() cannot share the NPU and simply fails to acquire.
            # Shared VDevice — see hailo_device.py. Creating our own would
            # take the single physical chip from Whisper.
            from ravensdr.hailo_device import get_vdevice
            self._vdevice = get_vdevice()
            if self._vdevice is None:
                raise RuntimeError("no Hailo VDevice")
            self._model = self._vdevice.create_infer_model(hef_path)
            self._model.input().set_format_type(FormatType.FLOAT32)
            self._model.output().set_format_type(FormatType.FLOAT32)
            self._configured = self._model.configure()
            self._bindings = self._configured.create_bindings()
            self._input_shape = tuple(self._model.input().shape)
            self._backend = "hailo"
            log.info("SEI model loaded on Hailo-8L: %s (input %s)",
                     hef_path, self._input_shape)
        except Exception as e:
            log.warning("SEI model: Hailo init failed (%s), using CPU fallback", e)
            self._backend = "cpu"

    @property
    def backend(self):
        return self._backend

    @property
    def is_active(self):
        return self._backend != "none"

    def identify(self, iq_samples, frequency_hz=0, modulation=None,
                 snr_db=0.0, duration_ms=0):
        """Identify emitter from raw IQ samples.

        Args:
            iq_samples: complex numpy array of IQ samples
            frequency_hz: center frequency
            modulation: classified modulation type from phase 16
            snr_db: estimated SNR
            duration_ms: transmission duration

        Returns:
            dict with identification result, or None if cannot identify
        """
        # Filter: require minimum SNR and duration
        if snr_db < MIN_SNR_DB:
            return None
        if duration_ms < MIN_DURATION_MS:
            return None
        if len(iq_samples) < IQ_WINDOW_SIZE:
            return None

        # Extract embedding
        embedding = self._get_embedding(iq_samples)
        if embedding is None:
            return None

        # Match against database
        match = self._match_emitter(embedding)
        now = datetime.datetime.now(datetime.timezone.utc).isoformat() + "Z"

        if match is not None:
            emitter_id, similarity = match
            # Re-identification: update existing emitter
            self._update_emitter(emitter_id, embedding, frequency_hz, now)

            record = self._emitters[emitter_id]
            result = {
                "event": "re_identified",
                "emitter_id": emitter_id,
                "label": record.label,
                "confidence": round(similarity, 3),
                "frequency_hz": frequency_hz,
                "timestamp": now,
                "observation_count": record.observation_count,
                "modulation": modulation,
            }
            self.emit_fn("emitter_identified", result)
            return result
        else:
            # New emitter: enroll
            emitter_id = self._enroll_emitter(embedding, frequency_hz, now)

            result = {
                "event": "new_emitter",
                "emitter_id": emitter_id,
                "label": None,
                "confidence": 1.0,
                "frequency_hz": frequency_hz,
                "timestamp": now,
                "observation_count": 1,
                "modulation": modulation,
            }
            self.emit_fn("new_emitter", result)
            return result

    def _get_embedding(self, iq_samples):
        """Get 128-dimensional embedding from IQ samples.

        Args:
            iq_samples: complex numpy array

        Returns:
            L2-normalized numpy array of shape (128,) or None
        """
        # Take first IQ_WINDOW_SIZE samples
        window = iq_samples[:IQ_WINDOW_SIZE]
        if len(window) < IQ_WINDOW_SIZE:
            window = np.pad(window, (0, IQ_WINDOW_SIZE - len(window)))

        if self._backend == "hailo" and self._model is not None:
            return self._infer_hailo(window)
        else:
            return self._infer_cpu(window)

    def _infer_hailo(self, iq_window):
        """Run Hailo inference to get embedding."""
        try:
            # HailoRT has no InferModel.infer() — see signal_classifier for the
            # same defect. Real API: configure/create_bindings/set_buffer/run.
            i = iq_window.real.astype(np.float32)
            q = iq_window.imag.astype(np.float32)

            # Hailo has no native 1D convolution, so a SEINet compiled for the
            # NPU must be reshaped to 2D (Conv1d k=7 -> Conv2d k=(1,7)), making
            # the input (1, 1, N, 2) NHWC rather than the (1, 2, N) the CPU path
            # uses. Adapt to whatever the loaded HEF actually declares instead
            # of assuming, so either compile works.
            if len(self._input_shape) == 4:
                input_data = np.stack([i, q], axis=-1)[None, None, :, :]
            else:
                input_data = np.stack([i, q], axis=0)[None, :, :]
            input_data = np.ascontiguousarray(input_data.astype(np.float32))

            with self._lock:
                out_buf = np.zeros(self._model.output().shape, dtype=np.float32)
                self._bindings.input().set_buffer(input_data)
                self._bindings.output().set_buffer(out_buf)
                self._configured.run([self._bindings], HAILO_TIMEOUT_MS)
                raw = self._bindings.output().get_buffer()

            embedding = np.asarray(raw).flatten()[:EMBEDDING_DIM]
            return l2_normalize(embedding.astype(np.float32))
        except Exception as e:
            log.warning("SEI Hailo inference failed: %s", e)
            return self._infer_cpu(iq_window)

    def _infer_cpu(self, iq_window):
        """CPU fallback: generate deterministic embedding from IQ features.

        Uses statistical features of the IQ signal to produce a repeatable
        embedding. Not as discriminative as the trained CNN but allows
        the pipeline to function without Hailo.
        """
        i_channel = iq_window.real.astype(np.float64)
        q_channel = iq_window.imag.astype(np.float64)
        mag = np.abs(iq_window).astype(np.float64)
        phase = np.angle(iq_window).astype(np.float64)

        features = []

        # Statistical moments of I, Q, magnitude, phase
        for sig in [i_channel, q_channel, mag, phase]:
            features.extend([
                np.mean(sig),
                np.std(sig),
                float(np.median(sig)),
                float(np.percentile(sig, 25)),
                float(np.percentile(sig, 75)),
            ])
        # Spectral features
        fft = np.fft.fft(iq_window)
        fft_mag = np.abs(fft)
        features.extend([
            np.mean(fft_mag),
            np.std(fft_mag),
            float(np.max(fft_mag)),
            float(np.argmax(fft_mag)),
        ])

        # I/Q correlation
        if np.std(i_channel) > 0 and np.std(q_channel) > 0:
            corr = np.corrcoef(i_channel, q_channel)[0, 1]
            features.append(float(corr) if np.isfinite(corr) else 0.0)
        else:
            features.append(0.0)

        # Zero-crossing rate
        zc_i = np.sum(np.diff(np.sign(i_channel)) != 0) / len(i_channel)
        zc_q = np.sum(np.diff(np.sign(q_channel)) != 0) / len(q_channel)
        features.extend([zc_i, zc_q])

        # Pad or truncate to EMBEDDING_DIM
        features = np.array(features, dtype=np.float32)
        if len(features) < EMBEDDING_DIM:
            features = np.pad(features, (0, EMBEDDING_DIM - len(features)))
        else:
            features = features[:EMBEDDING_DIM]

        return l2_normalize(features)

    def _match_emitter(self, embedding):
        """Find best matching emitter in database.

        Returns:
            (emitter_id, similarity) or None if no match above threshold
        """
        best_id = None
        best_sim = -1.0

        with self._db_lock:
            for eid, record in self._emitters.items():
                if record.embedding_centroid is None:
                    continue
                sim = cosine_similarity(embedding, record.embedding_centroid)
                if sim > best_sim:
                    best_sim = sim
                    best_id = eid

        if best_id is not None and best_sim >= self.match_threshold:
            return best_id, best_sim
        return None

    def _enroll_emitter(self, embedding, frequency_hz, timestamp):
        """Enroll a new emitter in the database."""
        with self._db_lock:
            emitter_id = f"EMITTER-{self._next_id:03d}"
            self._next_id += 1

            record = EmitterRecord(
                emitter_id=emitter_id,
                first_seen=timestamp,
                last_seen=timestamp,
                observation_count=1,
                frequency_history=[frequency_hz] if frequency_hz else [],
                embedding_centroid=embedding.copy(),
            )
            self._emitters[emitter_id] = record

        self._save_db()
        log.info("SEI: enrolled new emitter %s", emitter_id)
        return emitter_id

    def _update_emitter(self, emitter_id, embedding, frequency_hz, timestamp):
        """Update existing emitter with new observation."""
        with self._db_lock:
            record = self._emitters.get(emitter_id)
            if record is None:
                return

            record.last_seen = timestamp
            record.observation_count += 1

            # Update centroid with exponential moving average
            if record.embedding_centroid is not None:
                record.embedding_centroid = l2_normalize(
                    (1 - EMA_ALPHA) * record.embedding_centroid + EMA_ALPHA * embedding
                )
            else:
                record.embedding_centroid = embedding.copy()

            # Track frequency history (deduplicated)
            if frequency_hz and frequency_hz not in record.frequency_history:
                record.frequency_history.append(frequency_hz)

        self._save_db()

    # ── Database operations ──

    def _load_db(self):
        """Load emitter database from disk."""
        if not os.path.exists(self.db_path):
            log.info("SEI: no existing emitter database, starting fresh")
            return

        try:
            with open(self.db_path) as f:
                data = json.load(f)

            self._next_id = data.get("next_id", 1)
            for entry in data.get("emitters", []):
                record = EmitterRecord.from_dict(entry)
                self._emitters[record.emitter_id] = record

            log.info("SEI: loaded %d emitters from database", len(self._emitters))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("SEI: failed to load emitter database: %s", e)

    def _save_db(self):
        """Save emitter database to disk atomically."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        with self._db_lock:
            data = {
                "version": 1,
                "next_id": self._next_id,
                "emitters": [r.to_dict() for r in self._emitters.values()],
            }

        try:
            # Atomic write: write to temp file then rename
            fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(self.db_path), suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp_path, self.db_path)
            except Exception:
                os.unlink(tmp_path)
                raise
        except OSError as e:
            log.warning("SEI: failed to save emitter database: %s", e)

    def get_emitter(self, emitter_id):
        """Get a single emitter record."""
        with self._db_lock:
            record = self._emitters.get(emitter_id)
            if record:
                return record.to_dict()
        return None

    def list_emitters(self, limit=50, offset=0):
        """List emitters sorted by last_seen descending."""
        with self._db_lock:
            records = sorted(
                self._emitters.values(),
                key=lambda r: r.last_seen or "",
                reverse=True,
            )
            total = len(records)
            page = records[offset:offset + limit]
            return {
                "emitters": [r.to_dict() for r in page],
                "total": total,
            }

    def label_emitter(self, emitter_id, label):
        """Assign a human-readable label to an emitter."""
        with self._db_lock:
            record = self._emitters.get(emitter_id)
            if record is None:
                return False
            record.label = label
        self._save_db()
        return True

    def delete_emitter(self, emitter_id):
        """Remove an emitter from the fingerprint database."""
        with self._db_lock:
            if emitter_id not in self._emitters:
                return False
            del self._emitters[emitter_id]
        self._save_db()
        return True

    def get_status(self):
        """Return SEI model status for API."""
        with self._db_lock:
            total = len(self._emitters)
        return {
            "active": self.is_active,
            "backend": self._backend,
            "emitter_count": total,
            "embedding_dim": EMBEDDING_DIM,
            "match_threshold": self.match_threshold,
        }

    def apply_settings(self, settings):
        """Apply runtime settings from the Settings tab (see config.py)."""
        try:
            self.match_threshold = float(settings.get(
                "sei_match_threshold", self.match_threshold))
        except (TypeError, ValueError):
            pass

    def stop(self):
        """Clean up resources."""
        self._save_db()
        self._model = None
        if self._vdevice:
            self._vdevice = None
        log.info("SEI model stopped (%d emitters saved)", len(self._emitters))
