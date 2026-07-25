# Meteor scatter detector — IQ power monitor, threshold detector, trail classifier

import datetime
import json
import logging
import os
import struct
import time

import numpy as np

# Use REAL stdlib modules, not eventlet's green versions. The monitor thread does
# a blocking read on rtl_fm's stdout pipe; under a GREEN thread that blocking read
# stalls the entire eventlet hub (all HTTP/Socket.IO freezes), so the reader must
# be a real OS thread — same pattern as ais_receiver / adsb_receiver.
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
    threading = original("threading")
except ImportError:
    import subprocess
    import threading

log = logging.getLogger(__name__)

# Detection parameters
DEFAULT_THRESHOLD_DB = 10       # dB above noise floor to trigger detection
BASELINE_WINDOW_SEC = 30        # seconds for rolling noise floor average
MIN_BURST_MS = 50               # minimum burst duration (filter hardware noise)
MAX_BURST_SEC = 30              # maximum burst duration (flag as interference)
UNDERDENSE_THRESHOLD_SEC = 0.5  # boundary between underdense and overdense

# RTL-SDR parameters
SAMPLE_RATE = "12k"
OUTPUT_RATE = "11025"
DEFAULT_GAIN = 40
DEFAULT_FREQUENCY_HZ = 143050000  # Amateur meteor scatter calling frequency

# Log file
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LOG_FILE = os.path.join(DATA_DIR, "meteor_log.json")

# Environment config
METEOR_ENABLED = os.environ.get("METEOR_ENABLED", "false").lower() == "true"
METEOR_DUAL_DONGLE = os.environ.get("METEOR_DUAL_DONGLE", "false").lower() == "true"
METEOR_FREQUENCY = int(os.environ.get("METEOR_FREQUENCY", str(DEFAULT_FREQUENCY_HZ)))


class MeteorDetector:
    """Detects meteor scatter events by monitoring signal power on a carrier frequency."""

    def __init__(self, emit_fn=None, frequency_hz=None, device_index=0,
                 threshold_db=DEFAULT_THRESHOLD_DB):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self.frequency_hz = frequency_hz or METEOR_FREQUENCY
        self.device_index = device_index
        self.threshold_db = threshold_db

        self._running = False
        self._process = None
        self._thread = None
        self._events = []  # in-memory event buffer
        self._events_lock = threading.Lock()

        # Power tracking
        self._baseline_samples = []
        self._baseline_power_db = -100.0  # initial low baseline

        # Burst state
        self._in_burst = False
        self._burst_start = None
        self._burst_samples = []

    @property
    def is_running(self):
        return self._running

    @property
    def baseline_power_db(self):
        return self._baseline_power_db

    def start(self):
        """Start monitoring the carrier frequency for meteor scatter events."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        log.info("Meteor detector started on %.3f MHz (device %d, threshold %d dB)",
                 self.frequency_hz / 1e6, self.device_index, self.threshold_db)

    def stop(self):
        """Stop monitoring."""
        self._running = False
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("Meteor detector stopped")

    def get_events(self, limit=50, offset=0, shower=None, trail_type=None):
        """Return recent detection events, newest first."""
        with self._events_lock:
            filtered = self._events[:]

        if shower:
            filtered = [e for e in filtered if e.get("shower") == shower]
        if trail_type:
            filtered = [e for e in filtered if e.get("trail_type") == trail_type]

        filtered.sort(key=lambda e: e["timestamp"], reverse=True)
        return filtered[offset:offset + limit]

    def get_event_count(self):
        with self._events_lock:
            return len(self._events)

    def _monitor_loop(self):
        """Run rtl_fm and process audio samples for power detection."""
        cmd = self.build_rtl_fm_cmd()
        log.info("Meteor monitor: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log.error("rtl_fm not found — meteor detector cannot start")
            self._running = False
            return

        # Process raw signed 16-bit PCM samples
        sample_rate = 11025
        chunk_samples = 512  # ~46ms per chunk at 11025 Hz
        chunk_bytes = chunk_samples * 2  # 16-bit = 2 bytes per sample
        chunk_duration_sec = chunk_samples / sample_rate

        while self._running:
            raw = self._process.stdout.read(chunk_bytes)
            if not raw or len(raw) < chunk_bytes:
                if self._running:
                    log.warning("Meteor monitor: rtl_fm stream ended unexpectedly")
                break

            # Convert to numpy array of signed 16-bit integers
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64)

            # Compute power in dBm (relative)
            rms = np.sqrt(np.mean(samples ** 2))
            if rms > 0:
                power_db = 20 * np.log10(rms)
            else:
                power_db = -100.0

            # Update baseline (rolling average over BASELINE_WINDOW_SEC)
            self._update_baseline(power_db, chunk_duration_sec)

            # Check threshold
            excess_db = power_db - self._baseline_power_db
            now = datetime.datetime.utcnow()

            if excess_db >= self.threshold_db:
                if not self._in_burst:
                    self._in_burst = True
                    self._burst_start = now
                    self._burst_samples = []
                self._burst_samples.append(power_db)
            else:
                if self._in_burst:
                    # Burst ended — process it
                    self._process_burst(now)
                    self._in_burst = False
                    self._burst_start = None
                    self._burst_samples = []

        self._running = False

    def _update_baseline(self, power_db, chunk_duration_sec):
        """Update rolling noise floor baseline."""
        max_samples = int(BASELINE_WINDOW_SEC / chunk_duration_sec)
        self._baseline_samples.append(power_db)
        if len(self._baseline_samples) > max_samples:
            self._baseline_samples = self._baseline_samples[-max_samples:]

        # Use median for robustness against burst contamination
        if len(self._baseline_samples) >= 10:
            self._baseline_power_db = float(np.median(self._baseline_samples))

    def _process_burst(self, end_time):
        """Process a completed burst — filter, classify, emit event."""
        if not self._burst_start or not self._burst_samples:
            return

        duration = (end_time - self._burst_start).total_seconds()
        duration_ms = duration * 1000

        # Duration filtering
        if duration_ms < MIN_BURST_MS:
            return  # too short — hardware noise

        if duration > MAX_BURST_SEC:
            log.debug("Meteor: burst %.1fs flagged as interference (> %ds)",
                       duration, MAX_BURST_SEC)
            return  # too long — interference

        # Power analysis
        peak_power_db = float(np.max(self._burst_samples))
        mean_power_db = float(np.mean(self._burst_samples))

        # Trail classification
        trail_type = self._classify_trail(duration, self._burst_samples)

        # Build event
        event = {
            "timestamp": self._burst_start.strftime("%Y-%m-%dT%H:%M:%S.") +
                         f"{self._burst_start.microsecond // 1000:03d}Z",
            "duration_ms": round(duration_ms),
            "peak_power_dbm": round(peak_power_db, 1),
            "mean_power_dbm": round(mean_power_db, 1),
            "frequency_hz": self.frequency_hz,
            "doppler_offset_hz": 0,  # requires IQ analysis for accurate measurement
            "trail_type": trail_type,
            "shower": None,  # set by analyzer
            "shower_active": False,
        }

        # Store event
        with self._events_lock:
            self._events.append(event)
            # Cap in-memory buffer at 10000
            if len(self._events) > 10000:
                self._events = self._events[-10000:]

        # Persist to log file
        self._append_to_log(event)

        # Emit Socket.IO event
        self.emit_fn("meteor_detection", event)

        log.info("Meteor detected: %s %dms peak=%.1fdBm type=%s",
                 event["timestamp"], event["duration_ms"],
                 event["peak_power_dbm"], trail_type)

    @staticmethod
    def _classify_trail(duration_sec, power_samples):
        """Classify meteor trail type based on duration and power profile.

        Underdense: < 0.5s, exponential decay (sub-millimeter particles)
        Overdense: > 0.5s, plateau then decay (larger meteoroids)
        """
        if duration_sec < UNDERDENSE_THRESHOLD_SEC:
            return "underdense"
        return "overdense"

    def _append_to_log(self, event):
        """Append detection event to JSON log file."""
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            with open(LOG_FILE, "a") as f:
                f.write(json.dumps(event) + "\n")
        except OSError as e:
            log.warning("Failed to write meteor log: %s", e)

    def build_rtl_fm_cmd(self):
        """Build rtl_fm command for meteor carrier monitoring."""
        cmd = [
            "rtl_fm",
            "-f", str(self.frequency_hz),
            "-M", "fm",
            "-s", SAMPLE_RATE,
            "-r", OUTPUT_RATE,
            "-g", str(DEFAULT_GAIN),
        ]
        if self.device_index > 0:
            cmd.extend(["-d", str(self.device_index)])
        cmd.append("-")
        return cmd

    def load_events_from_log(self):
        """Load historical events from the JSON log file."""
        if not os.path.exists(LOG_FILE):
            return
        try:
            with open(LOG_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        event = json.loads(line)
                        self._events.append(event)
            log.info("Loaded %d historical meteor events from log", len(self._events))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Failed to load meteor log: %s", e)
