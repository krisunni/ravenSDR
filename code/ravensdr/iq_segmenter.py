# IQ transmission segmenter — detects transmission boundaries from continuous IQ stream
#
# Monitors IQ power level to detect when a transmitter keys up and keys down.
# Segments individual transmissions for signal classification (phase 16) and
# specific emitter identification (phase 17).

import collections
import datetime
import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

# Segmentation parameters
DEFAULT_THRESHOLD_DB = 10       # dB above noise floor to trigger
NOISE_FLOOR_WINDOW_SEC = 5.0   # rolling noise floor window
NOISE_FLOOR_PERCENTILE = 10    # low percentile, not the median — see _update_noise_floor
NOISE_FLOOR_RECOMPUTE_EVERY = 32  # chunks between percentile recomputes
MIN_TX_MS = 50                  # minimum transmission duration
MAX_TX_SEC = 30                 # maximum transmission duration
HYSTERESIS_MS = 100             # power must stay below threshold this long to end TX
RING_BUFFER_SEC = 10            # ring buffer size in seconds
DEFAULT_SAMPLE_RATE = 2400000   # 2.4 MHz default


class Segment:
    """A detected transmission segment."""

    __slots__ = ("iq_samples", "start_time", "duration_ms", "frequency_hz",
                 "snr_db", "peak_power_db", "mean_power_db")

    def __init__(self, iq_samples, start_time, duration_ms, frequency_hz,
                 snr_db, peak_power_db, mean_power_db):
        self.iq_samples = iq_samples
        self.start_time = start_time
        self.duration_ms = duration_ms
        self.frequency_hz = frequency_hz
        self.snr_db = snr_db
        self.peak_power_db = peak_power_db
        self.mean_power_db = mean_power_db

    def to_dict(self):
        return {
            "start_time": self.start_time.isoformat() + "Z",
            "duration_ms": self.duration_ms,
            "frequency_hz": self.frequency_hz,
            "snr_db": round(self.snr_db, 1),
            "peak_power_db": round(self.peak_power_db, 1),
            "mean_power_db": round(self.mean_power_db, 1),
            "sample_count": len(self.iq_samples),
        }


def compute_power_db(iq_samples):
    """Compute power in dB from complex IQ samples."""
    rms = np.sqrt(np.mean(np.abs(iq_samples) ** 2))
    if rms > 0:
        return 20 * np.log10(rms)
    return -100.0


class IQSegmenter:
    """Detects transmission boundaries from continuous IQ stream.

    Uses power thresholding with hysteresis to segment individual
    transmissions from a continuous IQ sample stream. Each detected
    segment is emitted to a callback for classification and fingerprinting.
    """

    def __init__(self, sample_rate=DEFAULT_SAMPLE_RATE, threshold_db=DEFAULT_THRESHOLD_DB,
                 on_segment=None):
        self.sample_rate = sample_rate
        self.threshold_db = threshold_db
        self.on_segment = on_segment  # callback(Segment)

        # Ring buffer for IQ data (10 seconds)
        self._buffer_size = int(RING_BUFFER_SEC * sample_rate)
        self._buffer = np.zeros(self._buffer_size, dtype=np.complex64)
        self._buffer_pos = 0  # write position
        self._buffer_filled = 0  # total samples written (may wrap)

        # Noise floor tracking.
        # deque, not a list: this is appended 2,344x/sec at 2.4 MS/s, and the old
        # `self._noise_samples[-N:]` reslice reallocated an 11,718-element list
        # every time — ~27M element-copies/sec, which with a median per chunk put
        # feed() at 237% of one core.
        self._noise_floor_db = -100.0
        self._noise_window_samples = int(NOISE_FLOOR_WINDOW_SEC * sample_rate / 1024)
        self._noise_samples = collections.deque(maxlen=self._noise_window_samples)
        self._chunks_since_floor = 0
        # Until a real floor exists, -100.0 dB is a placeholder that ANY input
        # clears by 60 dB. Detection must not run against it, and the freeze
        # below must not latch on it — see _check_threshold.
        self._floor_ready = False

        # Transmission state
        self._in_tx = False
        self._tx_start_time = None
        self._tx_start_pos = 0
        self._tx_power_samples = []
        self._below_threshold_count = 0

        # Chunk tracking
        self._chunk_samples = 1024  # process in 1024-sample chunks
        self._hysteresis_chunks = max(1, int(
            (HYSTERESIS_MS / 1000.0) * sample_rate / self._chunk_samples
        ))
        self._min_tx_chunks = max(1, int(
            (MIN_TX_MS / 1000.0) * sample_rate / self._chunk_samples
        ))
        self._max_tx_chunks = int(
            MAX_TX_SEC * sample_rate / self._chunk_samples
        )
        self._tx_chunk_count = 0

        self._frequency_hz = 0
        self._lock = threading.Lock()

    def set_frequency(self, frequency_hz):
        """Set current center frequency for segment metadata."""
        self._frequency_hz = frequency_hz

    def apply_settings(self, settings):
        """Apply runtime settings from the Settings tab (see config.py)."""
        try:
            self.threshold_db = float(settings.get(
                "segmenter_threshold_db", self.threshold_db))
        except (TypeError, ValueError):
            pass

    def feed(self, iq_samples):
        """Feed IQ samples into the segmenter.

        Args:
            iq_samples: complex numpy array of IQ samples
        """
        with self._lock:
            self._write_to_buffer(iq_samples)
            self._process_chunks(iq_samples)

    def _write_to_buffer(self, iq_samples):
        """Write IQ samples to ring buffer."""
        n = len(iq_samples)
        if n >= self._buffer_size:
            # More data than buffer — just keep the tail
            self._buffer[:] = iq_samples[-self._buffer_size:]
            self._buffer_pos = 0
            self._buffer_filled = self._buffer_size
        else:
            end = self._buffer_pos + n
            if end <= self._buffer_size:
                self._buffer[self._buffer_pos:end] = iq_samples
            else:
                first = self._buffer_size - self._buffer_pos
                self._buffer[self._buffer_pos:] = iq_samples[:first]
                self._buffer[:n - first] = iq_samples[first:]
            self._buffer_pos = end % self._buffer_size
            self._buffer_filled = min(self._buffer_filled + n, self._buffer_size)

    def _process_chunks(self, iq_samples):
        """Process IQ samples in chunks for power detection."""
        chunk_size = self._chunk_samples
        for i in range(0, len(iq_samples) - chunk_size + 1, chunk_size):
            chunk = iq_samples[i:i + chunk_size]
            power_db = compute_power_db(chunk)
            self._update_noise_floor(power_db)
            self._check_threshold(power_db, chunk)

    def _update_noise_floor(self, power_db):
        """Update the rolling noise floor from quiet chunks only.

        Two bugs lived here and they compounded.

        The floor was updated on EVERY chunk, including chunks belonging to the
        transmission being measured, so a long signal walked its own floor
        upward until it no longer cleared the threshold. Measured: -37.0 dB on
        noise, +3.0 dB after 20s of carrier — the floor had become the signal. A
        single 20s transmission came out as a 0.1s fragment, a 2.6s fragment,
        then 17.4s of deafness, reporting ~0 dB SNR for something 40 dB up. That
        bogus SNR silently disabled sei_model.identify (needs 15 dB) and
        collect_sample (needs 10 dB), and those fragments are where the 50 MB
        .npy files on disk came from.

        It also used the median — the level exceeded half the time, which on a
        busy channel IS the signal. transcriber.NoiseFloorTracker documents both
        problems and uses a low percentile over quiet frames; this is the same
        approach, applied where it was missing.
        """
        # Freeze only once a real floor exists. Freezing before that deadlocks
        # cold start: the placeholder floor makes chunk 1 look like a
        # transmission, _in_tx latches, and the floor can then never be learned.
        if self._in_tx and self._floor_ready:
            return                      # never measure the floor from the signal

        self._noise_samples.append(power_db)   # deque(maxlen) evicts for us
        self._chunks_since_floor += 1

        # The percentile spans 5s, so recomputing it 2,344x/sec buys nothing — it
        # cannot move meaningfully between adjacent chunks. The FIRST estimate is
        # exempt: throttling it too would leave _floor_ready false for 32 chunks,
        # during which detection is disabled and a transmission starting inside
        # the warm-up is missed entirely.
        due = (not self._floor_ready
               or self._chunks_since_floor >= NOISE_FLOOR_RECOMPUTE_EVERY)
        if len(self._noise_samples) >= 5 and due:
            self._chunks_since_floor = 0
            self._noise_floor_db = float(
                np.percentile(self._noise_samples, NOISE_FLOOR_PERCENTILE))
            self._floor_ready = True

    def _check_threshold(self, power_db, chunk):
        """Check if power exceeds threshold and manage TX state."""
        if not self._floor_ready:
            # No floor yet, so "10 dB above the floor" is meaningless. Detecting
            # against the -100 dB placeholder fires on the first chunk of noise
            # and reports the whole warm-up as one transmission.
            return

        excess = power_db - self._noise_floor_db
        now = datetime.datetime.now(datetime.timezone.utc)

        if excess >= self.threshold_db:
            self._below_threshold_count = 0

            if not self._in_tx:
                # Transmission start
                self._in_tx = True
                self._tx_start_time = now
                self._tx_start_pos = self._buffer_pos
                self._tx_power_samples = []
                self._tx_chunk_count = 0

            self._tx_power_samples.append(power_db)
            self._tx_chunk_count += 1

            # Check max duration
            if self._tx_chunk_count >= self._max_tx_chunks:
                self._end_transmission(now)
        else:
            if self._in_tx:
                self._below_threshold_count += 1
                self._tx_power_samples.append(power_db)
                self._tx_chunk_count += 1

                if self._below_threshold_count >= self._hysteresis_chunks:
                    self._end_transmission(now)

    def _end_transmission(self, end_time):
        """Process a completed transmission segment."""
        if not self._in_tx or self._tx_start_time is None:
            self._in_tx = False
            return

        self._in_tx = False

        # Check minimum duration
        if self._tx_chunk_count < self._min_tx_chunks:
            return

        duration_ms = round(
            self._tx_chunk_count * self._chunk_samples / self.sample_rate * 1000
        )

        # Extract IQ samples from ring buffer
        total_samples = self._tx_chunk_count * self._chunk_samples
        if total_samples > self._buffer_filled:
            total_samples = self._buffer_filled

        iq_samples = self._extract_from_buffer(total_samples)

        # Power stats
        if self._tx_power_samples:
            peak_power = float(np.max(self._tx_power_samples))
            mean_power = float(np.mean(self._tx_power_samples))
        else:
            peak_power = -100.0
            mean_power = -100.0

        snr = mean_power - self._noise_floor_db

        segment = Segment(
            iq_samples=iq_samples,
            start_time=self._tx_start_time,
            duration_ms=duration_ms,
            frequency_hz=self._frequency_hz,
            snr_db=snr,
            peak_power_db=peak_power,
            mean_power_db=mean_power,
        )

        # Emit to callback
        if self.on_segment:
            try:
                self.on_segment(segment)
            except Exception as e:
                log.debug("Segment callback error: %s", e)

        # Reset state
        self._tx_start_time = None
        self._tx_power_samples = []
        self._tx_chunk_count = 0
        self._below_threshold_count = 0

    def _extract_from_buffer(self, n_samples):
        """Extract the last n_samples from the ring buffer."""
        n = min(n_samples, self._buffer_filled)
        end = self._buffer_pos
        start = (end - n) % self._buffer_size

        if start < end:
            return self._buffer[start:end].copy()
        else:
            return np.concatenate([
                self._buffer[start:],
                self._buffer[:end],
            ])

    @property
    def noise_floor_db(self):
        return self._noise_floor_db

    @property
    def in_transmission(self):
        return self._in_tx

    def reset(self):
        """Reset segmenter state."""
        with self._lock:
            self._in_tx = False
            self._tx_start_time = None
            self._tx_power_samples = []
            self._tx_chunk_count = 0
            self._below_threshold_count = 0
            self._noise_samples.clear()
            self._floor_ready = False
            self._noise_floor_db = -100.0
            self._buffer_filled = 0
            self._buffer_pos = 0
