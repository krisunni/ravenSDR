# InputSource abstraction — SDR or web stream

import logging
import queue

# Use the REAL subprocess module, not eventlet's green version.
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
except ImportError:
    import subprocess

from ravensdr.tuner import Tuner
from ravensdr.stream_source import StreamSource

log = logging.getLogger(__name__)


def detect_sdr():
    """Check if an RTL-SDR device is connected (without opening it exclusively)."""
    # First try lsusb — works even if another process (dump1090) holds the device
    try:
        result = subprocess.run(
            ["lsusb"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        # RTL-SDR Blog V4 uses 0bda:2838 (Realtek RTL2838)
        if "0bda:2838" in result.stdout or "RTL2838" in result.stdout:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Fallback to rtl_test if lsusb not available
    try:
        result = subprocess.run(
            ["rtl_test", "-t"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


class InputSource:
    """Unified abstraction over Tuner (SDR) and StreamSource (web stream)."""

    def __init__(self, mode):
        self.mode = mode  # "SDR" or "WEBSTREAM"
        self.pcm_queue = queue.Queue(maxsize=200)
        self.audio_queue = queue.Queue(maxsize=200)
        self.current_preset = None
        self.sdr_connected = (mode == "SDR")
        self._error_callback = None
        self._apt_mode = False
        self._apt_saved_preset = None

        if mode == "SDR":
            self._source = Tuner(self.pcm_queue, self.audio_queue)
        else:
            self._source = StreamSource(self.pcm_queue, self.audio_queue)

    def set_error_callback(self, callback):
        """Set callback for error/recovery notifications: callback(event, data)."""
        self._error_callback = callback

    def tune(self, preset):
        """Tune to a preset. Uses stream_url in WEBSTREAM mode, freq in SDR mode."""
        if self._apt_mode:
            log.warning("Cannot tune — SDR is in APT satellite recording mode")
            return False
        self.current_preset = preset
        if self.mode == "WEBSTREAM":
            stream_url = preset.get("stream_url")
            if not stream_url:
                log.error("Preset '%s' has no stream_url for web stream mode",
                          preset.get("label"))
                return False
            self._source.connect(stream_url)
        else:
            # Apply preset-level settings before tuning (tune restarts rtl_fm)
            if "squelch" in preset:
                self._source.squelch = preset["squelch"]
            if "sample_rate" in preset:
                self._source.sample_rate = preset["sample_rate"]
            if "deemp" in preset:
                self._source.deemp = preset["deemp"]
            self._source.tune(preset["freq"], preset.get("mode", "fm"))
        return True

    def stop(self):
        self._source.stop()
        self.current_preset = None

    @property
    def is_running(self):
        return self._source.is_running

    def poll(self):
        return self._source.poll()

    def check_sdr_connected(self):
        """Check if SDR hardware is still plugged in. Returns True/False."""
        was_connected = self.sdr_connected
        self.sdr_connected = detect_sdr()

        if was_connected and not self.sdr_connected:
            log.warning("SDR disconnected")
            if self._error_callback:
                self._error_callback("sdr_disconnected", {
                    "message": "SDR dongle disconnected. Plug it back in to auto-recover."
                })

        elif not was_connected and self.sdr_connected:
            log.info("SDR reconnected")
            if self._error_callback:
                self._error_callback("sdr_reconnected", {
                    "message": "SDR dongle reconnected."
                })

        return self.sdr_connected

    def restart(self):
        """Restart the current source (retry after crash)."""
        if not self.current_preset:
            log.warning("Cannot restart — no preset selected")
            return False
        preset = self.current_preset
        self._source.stop()
        return self.tune(preset)

    def set_squelch(self, level):
        if self.mode == "SDR":
            self._source.set_squelch(level)

    def set_gain(self, value):
        if self.mode == "SDR":
            self._source.set_gain(value)

    def set_sample_rate(self, value):
        if self.mode == "SDR":
            self._source.set_sample_rate(value)

    def set_deemp(self, value):
        if self.mode == "SDR":
            self._source.set_deemp(value)

    def set_ppm(self, value):
        if self.mode == "SDR":
            self._source.set_ppm(value)

    def set_direct_sampling(self, value):
        if self.mode == "SDR":
            self._source.set_direct_sampling(value)

    @property
    def squelch(self):
        if self.mode == "SDR":
            return self._source.squelch
        return 0

    @property
    def gain(self):
        if self.mode == "SDR":
            return self._source.gain
        return "N/A"

    @property
    def sample_rate(self):
        if self.mode == "SDR":
            return self._source.sample_rate
        return None

    @property
    def effective_sample_rate(self):
        if self.mode == "SDR":
            return self._source.effective_sample_rate
        return "N/A"

    @property
    def deemp(self):
        if self.mode == "SDR":
            return self._source.deemp
        return None

    @property
    def effective_deemp(self):
        if self.mode == "SDR":
            return self._source.effective_deemp
        return False

    @property
    def ppm(self):
        if self.mode == "SDR":
            return self._source.ppm
        return 0

    @property
    def direct_sampling(self):
        if self.mode == "SDR":
            return self._source.direct_sampling
        return 0

    @property
    def apt_mode(self):
        return self._apt_mode

    def enter_apt_mode(self, frequency_mhz):
        """Pause normal scanning and dedicate SDR to APT satellite recording."""
        if self.mode != "SDR":
            log.warning("APT mode only supported in SDR mode")
            return False
        if self._apt_mode:
            log.warning("Already in APT mode")
            return False

        self._apt_saved_preset = self.current_preset
        was_running = self.is_running
        if was_running:
            self._source.stop()

        self._apt_mode = True
        log.info("Entered APT mode — SDR dedicated to %s", frequency_mhz)

        if self._error_callback:
            self._error_callback("apt_mode_entered", {
                "message": f"SDR in satellite recording mode ({frequency_mhz})",
            })
        return True

    def exit_apt_mode(self):
        """Exit APT recording mode and resume normal scanning."""
        if not self._apt_mode:
            return

        self._apt_mode = False
        log.info("Exited APT mode")

        # Resume previous preset if one was active
        if self._apt_saved_preset:
            preset = self._apt_saved_preset
            self._apt_saved_preset = None
            self.tune(preset)
            log.info("Resumed scanning: %s", preset.get("label", ""))
        else:
            self._apt_saved_preset = None

        if self._error_callback:
            self._error_callback("apt_mode_exited", {
                "message": "SDR satellite recording complete — normal scanning resumed",
            })
