# ADS-B receiver — dump1090 process manager + JSON flight poller

import logging
import os
import threading
import time

try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
except ImportError:
    import subprocess

try:
    import eventlet
    _HAS_EVENTLET = True
except ImportError:
    _HAS_EVENTLET = False

import requests

log = logging.getLogger(__name__)

ADSB_DECODER_JSON = "http://localhost:8080/data/aircraft.json"

# Config from environment
ADSB_ENABLED = os.environ.get("ADSB_ENABLED", "true").lower() == "true"
ADSB_DUAL_DONGLE = os.environ.get("ADSB_DUAL_DONGLE", "false").lower() == "true"
ADSB_SCAN_INTERVAL = int(os.environ.get("ADSB_SCAN_INTERVAL", "60"))
ADSB_SCAN_DURATION = int(os.environ.get("ADSB_SCAN_DURATION", "30"))


class AdsbReceiver:
    """Manages dump1090 process and polls aircraft JSON."""

    def __init__(self, device_index=0, dual_dongle=False):
        self.device_index = device_index
        self.dual_dongle = dual_dongle
        self.process = None
        self.flights = []
        self._poll_thread = None
        self._running = False

    @property
    def is_running(self):
        return self._running

    def start(self):
        """Start dump1090 subprocess on configured device index."""
        if self._running:
            return

        cmd = [
            "dump1090-mutability",
            "--device-index", str(self.device_index),
            "--net",
            "--quiet",
        ]
        try:
            self.process = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log.error("dump1090-mutability not found — is it installed?")
            return

        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        log.info("dump1090 started on device %d", self.device_index)

    def stop(self):
        """Stop dump1090 and polling."""
        self._running = False
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=3)
            self._poll_thread = None
        log.info("dump1090 stopped")

    def _poll_loop(self):
        """Poll dump1090 JSON endpoint every 2 seconds."""
        while self._running:
            try:
                resp = requests.get(ADSB_DECODER_JSON, timeout=2)
                data = resp.json()
                self.flights = data.get("aircraft", [])
            except Exception as e:
                log.debug("dump1090 poll error: %s", e)
            time.sleep(2)

    def get_flights(self):
        """Return current flight list."""
        return list(self.flights)


class AdsbScanScheduler:
    """Single-dongle time-sharing: pauses rtl_fm for ADS-B scan windows."""

    def __init__(self, receiver, input_source,
                 scan_interval=ADSB_SCAN_INTERVAL,
                 scan_duration=ADSB_SCAN_DURATION):
        self.receiver = receiver
        self.input_source = input_source
        self.scan_interval = scan_interval
        self.scan_duration = scan_duration
        self._running = False
        self._thread = None
        self._scanning = False
        self._status_callback = None

    @property
    def is_scanning(self):
        return self._scanning

    def set_status_callback(self, callback):
        """Set callback(scanning: bool) for UI status updates."""
        self._status_callback = callback

    def _sleep(self, seconds):
        """Sleep using eventlet if available, else time.sleep."""
        if _HAS_EVENTLET:
            eventlet.sleep(seconds)
        else:
            time.sleep(seconds)

    def start(self):
        if self._running:
            return
        self._running = True
        if _HAS_EVENTLET:
            self._thread = eventlet.spawn(self._scan_loop)
        else:
            self._thread = threading.Thread(target=self._scan_loop, daemon=True)
            self._thread.start()
        log.info("ADS-B scan scheduler started (interval=%ds, duration=%ds)",
                 self.scan_interval, self.scan_duration)

    def stop(self):
        self._running = False
        if self._scanning:
            self.receiver.stop()
            self._scanning = False
        if self._thread is not None:
            if _HAS_EVENTLET and hasattr(self._thread, 'wait'):
                try:
                    self._thread.wait()
                except Exception:
                    pass
            elif hasattr(self._thread, 'join'):
                self._thread.join(timeout=5)
            self._thread = None

    def _scan_loop(self):
        while self._running:
            # Wait for scan interval
            for _ in range(self.scan_interval):
                if not self._running:
                    return
                self._sleep(1)

            if not self._running:
                return

            # Only scan when tuned to an aviation preset
            preset = self.input_source.current_preset
            if not preset or preset.get("category") != "aviation":
                continue

            # Pause rtl_fm, start ADS-B scan
            log.info("ADS-B scan window starting — pausing rtl_fm")
            self._scanning = True
            if self._status_callback:
                self._status_callback(True)

            was_running = self.input_source.is_running
            preset = self.input_source.current_preset
            if was_running:
                self.input_source.stop()
                # Restore preset reference so we can resume
                self.input_source.current_preset = preset

            self.receiver.start()

            # Scan for configured duration
            for _ in range(self.scan_duration):
                if not self._running:
                    self.receiver.stop()
                    return
                self._sleep(1)

            # Stop ADS-B, resume rtl_fm
            self.receiver.stop()
            self._scanning = False
            if self._status_callback:
                self._status_callback(False)

            if was_running and preset:
                log.info("ADS-B scan complete — resuming rtl_fm")
                self.input_source.tune(preset)
