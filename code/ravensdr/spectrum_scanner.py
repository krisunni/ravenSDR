# Spectrum survey — sweep a band, find what is transmitting on it.
#
# Every time this node has had to answer "is there anything on that frequency?"
# the answer came from running rtl_power by hand in a scratch directory and
# eyeballing a CSV. That is the wrong place for it: the node already owns the
# radio, knows its own presets, and has a browser attached. This makes the
# survey a first-class mode.
#
# It seizes the dongle for the length of a sweep, so it behaves like the other
# dedicated modes: stop the audio pipeline first, run, hand the radio back.

import logging
import time

import numpy as np

# Real stdlib, not eventlet's green versions: the reader does a blocking
# readline on rtl_power's stdout, and a green thread doing that stalls the whole
# hub. Same reasoning as subprocess_decoder and tuner.
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
    threading = original("threading")
except ImportError:
    import subprocess
    import threading

log = logging.getLogger(__name__)

PROC_NAME = "rtl_power"

# Named bands spanning what the RTL-SDR Blog V4 can reach (500 kHz - 1.7 GHz).
# Bin sizes are chosen so a sweep finishes in a sane time while still resolving
# the channel spacing that band actually uses: 10 kHz for MW broadcast, 200 kHz
# for FM, 25 kHz for the VHF/UHF land-mobile bands.
BANDS = [
    {"id": "mw",        "label": "MW broadcast",     "low": 520_000,     "high": 1_710_000,     "bin": 2_000,   "note": "AM stations, 10 kHz spacing"},
    {"id": "hf-49m",    "label": "Shortwave 49m",    "low": 5_900_000,   "high": 6_200_000,     "bin": 2_000,   "note": "International broadcast — needs an HF antenna"},
    {"id": "hf-31m",    "label": "Shortwave 31m",    "low": 9_400_000,   "high": 9_900_000,     "bin": 2_000,   "note": "International broadcast — needs an HF antenna"},
    {"id": "cb",        "label": "CB 27 MHz",        "low": 26_960_000,  "high": 27_410_000,    "bin": 2_000,   "note": "Citizens band, 40 channels"},
    {"id": "6m",        "label": "6m ham",           "low": 50_000_000,  "high": 54_000_000,    "bin": 10_000,  "note": "Amateur"},
    {"id": "fm",        "label": "FM broadcast",     "low": 87_500_000,  "high": 108_000_000,   "bin": 50_000,  "note": "Commercial FM, 200 kHz spacing"},
    {"id": "air",       "label": "Airband",          "low": 118_000_000, "high": 137_000_000,   "bin": 25_000,  "note": "Aviation AM voice"},
    {"id": "sat",       "label": "Weather sats",     "low": 136_000_000, "high": 138_000_000,   "bin": 10_000,  "note": "NOAA APT, Meteor"},
    {"id": "2m",        "label": "2m ham",           "low": 144_000_000, "high": 148_000_000,   "bin": 12_500,  "note": "Amateur, repeaters and APRS"},
    {"id": "marine",    "label": "Marine VHF",       "low": 156_000_000, "high": 162_100_000,   "bin": 12_500,  "note": "Ship traffic, AIS, NOAA weather"},
    {"id": "vhf-hi",    "label": "VHF land mobile",  "low": 150_000_000, "high": 174_000_000,   "bin": 12_500,  "note": "Public safety, business, pagers"},
    {"id": "uhf-lo",    "label": "UHF land mobile",  "low": 450_000_000, "high": 470_000_000,   "bin": 12_500,  "note": "Public safety, business"},
    {"id": "frs",       "label": "FRS / GMRS",       "low": 462_000_000, "high": 468_000_000,   "bin": 12_500,  "note": "Handheld radios"},
    {"id": "70cm",      "label": "70cm ham",         "low": 420_000_000, "high": 450_000_000,   "bin": 25_000,  "note": "Amateur"},
    {"id": "ism-433",   "label": "ISM 433 MHz",      "low": 433_000_000, "high": 435_000_000,   "bin": 10_000,  "note": "Sensors, remotes, TPMS"},
    {"id": "ism-915",   "label": "ISM 915 MHz",      "low": 902_000_000, "high": 928_000_000,   "bin": 100_000, "note": "Meters, sensors, LoRa"},
    {"id": "gsm-850",   "label": "Cellular 850",     "low": 824_000_000, "high": 894_000_000,   "bin": 200_000, "note": "Mobile network downlink"},
    {"id": "adsb",      "label": "ADS-B 1090",       "low": 1_085_000_000, "high": 1_095_000_000, "bin": 100_000, "note": "Aircraft transponders"},
    {"id": "everything", "label": "Everything (slow)", "low": 500_000,   "high": 1_700_000_000, "bin": 1_000_000, "note": "Full tuning range, coarse — takes a long time"},
]

BANDS_BY_ID = {b["id"]: b for b in BANDS}

DEFAULT_INTEGRATION_S = 4
PEAK_MIN_OVER_FLOOR_DB = 3.0   # below this a "peak" is indistinguishable from noise
MAX_PEAKS = 40


class SpectrumScanner:
    """Run rtl_power over a band and report the peaks found."""

    def __init__(self, emit_fn=None, device_index=0):
        self.emit_fn = emit_fn or (lambda *a, **k: None)
        self.device_index = device_index
        self._process = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()

        self._band = None
        self._bins = {}            # freq_hz -> dBm
        self._started_at = None
        self._finished_at = None
        self._progress = 0.0
        self._last_error = None
        self._last_emit = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────

    @property
    def is_running(self):
        return self._running

    def start(self, band_id=None, low=None, high=None, bin_hz=None,
              gain=None, integration_s=DEFAULT_INTEGRATION_S):
        """Begin a sweep. Caller MUST have released the SDR first."""
        if self._running:
            return False, "a sweep is already running"

        band = dict(BANDS_BY_ID.get(band_id) or {})
        if band:
            low = low or band["low"]
            high = high or band["high"]
            bin_hz = bin_hz or band["bin"]
        if not (low and high and bin_hz):
            return False, "need a band id, or low/high/bin"
        if high <= low:
            return False, "high must be above low"

        self._kill_lingering()

        band.setdefault("id", "custom")
        band.setdefault("label", f"{low/1e6:.3f}-{high/1e6:.3f} MHz")
        band.update({"low": low, "high": high, "bin": bin_hz})

        cmd = [
            "rtl_power",
            "-f", f"{low}:{high}:{bin_hz}",
            "-d", str(self.device_index),
            "-i", str(integration_s),
            "-1",                       # single shot; we want a survey, not a monitor
            "-",
        ]
        if gain is not None:
            cmd[1:1] = ["-g", str(gain)]

        log.info("Spectrum sweep: %s (%.3f-%.3f MHz, %d Hz bins)",
                 band["label"], low / 1e6, high / 1e6, bin_hz)
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1)
        except FileNotFoundError:
            self._last_error = "rtl_power is not installed"
            return False, self._last_error
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            return False, self._last_error

        with self._lock:
            self._band = band
            self._bins = {}
            self._started_at = time.time()
            self._finished_at = None
            self._progress = 0.0
            self._last_error = None
        self._running = True
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self.emit_fn("sweep_started", self.snapshot())
        return True, None

    def stop(self):
        """Cancel a running sweep."""
        if not self._running:
            return
        self._running = False
        proc = self._process
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._process = None
        log.info("Spectrum sweep stopped")

    def _kill_lingering(self):
        """Clear a leftover rtl_power that would hold the device."""
        try:
            subprocess.run(["killall", "-q", PROC_NAME],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
        except Exception:
            pass

    # ── the sweep itself ─────────────────────────────────────────────────

    def _reader(self):
        """Parse rtl_power CSV as it streams.

        Each row is one contiguous chunk of the range, so a wide sweep arrives
        as many rows and we can report real progress rather than a spinner.
        """
        try:
            for line in self._process.stdout:
                if not self._running:
                    break
                self._ingest(line)
        except (ValueError, OSError):
            pass

        rc = None
        try:
            rc = self._process.poll()
            if rc is None:
                self._process.wait(timeout=5)
                rc = self._process.returncode
        except Exception:
            pass

        if self._running and rc not in (0, None):
            detail = ""
            try:
                detail = (self._process.stderr.read() or "").strip()[:200]
            except Exception:
                pass
            self._last_error = detail or f"rtl_power exited {rc}"
            log.error("Spectrum sweep failed: %s", self._last_error)

        self._running = False
        self._finished_at = time.time()
        self._progress = 1.0
        self.emit_fn("sweep_complete", self.snapshot(include_bins=True))
        log.info("Spectrum sweep complete: %d bins, %d peaks",
                 len(self._bins), len(self.peaks()))

    def _ingest(self, line):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            return
        try:
            low = float(parts[2])
            step = float(parts[4])
            vals = [float(p) for p in parts[6:] if p not in ("", "-nan", "nan")]
        except ValueError:
            return

        with self._lock:
            for i, v in enumerate(vals):
                self._bins[int(low + i * step)] = v
            band = self._band or {}
            span = max(1.0, band.get("high", 1) - band.get("low", 0))
            # Progress from how far up the range rtl_power has reached, which is
            # monotonic even though bins arrive in chunks.
            reached = (low + len(vals) * step) - band.get("low", 0)
            self._progress = max(self._progress, min(1.0, reached / span))

        now = time.time()
        if now - self._last_emit > 0.5:
            self._last_emit = now
            self.emit_fn("sweep_progress", self.snapshot())

    # ── results ──────────────────────────────────────────────────────────

    def noise_floor(self):
        with self._lock:
            vals = list(self._bins.values())
        if not vals:
            return None
        return float(np.median(vals))

    def peaks(self, min_over_floor=PEAK_MIN_OVER_FLOOR_DB, limit=MAX_PEAKS):
        """Local maxima that stand clear of the band's own noise floor.

        Compared against the MEDIAN of the band rather than an absolute dBm,
        because the floor moves with gain, antenna and band. A signal is
        something louder than its neighbours, not something louder than a
        number chosen in advance.
        """
        with self._lock:
            items = sorted(self._bins.items())
        if len(items) < 5:
            return []
        freqs = [f for f, _ in items]
        vals = np.array([v for _, v in items], dtype=np.float32)
        floor = float(np.median(vals))

        found = []
        i = 1
        while i < len(vals) - 1:
            v = vals[i]
            if v - floor < min_over_floor:
                i += 1
                continue
            # Walk to the top of this hump, then past it, so one transmitter
            # produces one entry rather than one per bin above threshold.
            j = i
            while j < len(vals) - 1 and vals[j + 1] >= vals[j]:
                j += 1
            top = j
            while j < len(vals) - 1 and vals[j + 1] < vals[j]:
                j += 1
            found.append({
                "freq_hz": int(freqs[top]),
                "db": round(float(vals[top]), 1),
                "over_floor_db": round(float(vals[top] - floor), 1),
            })
            i = max(j, top + 1)

        found.sort(key=lambda p: -p["over_floor_db"])
        return found[:limit]

    def snapshot(self, include_bins=False):
        with self._lock:
            band = dict(self._band) if self._band else None
            n_bins = len(self._bins)
            started = self._started_at
            finished = self._finished_at
            progress = self._progress
            err = self._last_error
        out = {
            "running": self._running,
            "band": band,
            "bins_collected": n_bins,
            "progress": round(progress, 3),
            "started_at": started,
            "finished_at": finished,
            "elapsed_s": round((finished or time.time()) - started, 1) if started else None,
            "noise_floor_db": (round(self.noise_floor(), 1)
                               if n_bins else None),
            "last_error": err,
        }
        if include_bins or (not self._running and n_bins):
            out["peaks"] = self.peaks()
        if include_bins:
            with self._lock:
                items = sorted(self._bins.items())
            # Sent as parallel arrays: a list of {freq, db} objects for a wide
            # sweep is megabytes of JSON key names for no added meaning.
            out["spectrum"] = {
                "freqs": [f for f, _ in items],
                "dbs": [round(v, 1) for _, v in items],
            }
        return out
