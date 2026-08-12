# InputSource abstraction — SDR or web stream

import logging
import queue

# Use the REAL subprocess and queue modules, not eventlet's green versions.
#
# Both queues are written by real OS threads (tuner._read_loop,
# stream_source._read_loop) and read by the transcriber (also a real thread)
# and the /audio-stream generator (a greenthread). A green queue cannot carry
# that: eventlet's Queue signals waiters by switching greenlets, which from a
# foreign OS thread raises "greenlet.error: Cannot switch to a different
# thread". Reproduced — the consumer never saw a producer notify and woke only
# on its own timeout, so /audio-stream delivered audio in 5-SECOND BURSTS
# (get(timeout=5)) and the transcriber added ~1s of latency per read.
#
# A real queue fixes the wakeups, but means no greenthread may ever block on
# one — see audio_router.audio_stream_generator, which polls and yields.
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
    queue = original("queue")
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
        self._wefax_mode = False
        self._wefax_saved_preset = None
        self._meteor_mode = False
        self._meteor_saved_preset = None

        if mode == "SDR":
            self._source = Tuner(self.pcm_queue, self.audio_queue)
        else:
            self._source = StreamSource(self.pcm_queue, self.audio_queue)

    def set_error_callback(self, callback):
        """Set callback for error/recovery notifications: callback(event, data)."""
        self._error_callback = callback

    def set_iq_callback(self, callback):
        """Set callback for raw IQ chunks (SDR pyrtlsdr mode only).

        callback(iq_samples, frequency_hz) called for each IQ chunk.
        """
        if self.mode == "SDR" and hasattr(self._source, 'set_iq_callback'):
            self._source.set_iq_callback(callback)

    def tune(self, preset):
        """Tune to a preset. Uses stream_url in WEBSTREAM mode, freq in SDR mode."""
        if self._apt_mode:
            log.warning("Cannot tune — SDR is in APT satellite recording mode")
            return False
        if self._wefax_mode:
            log.warning("Cannot tune — SDR is in WEFAX recording mode")
            return False
        if self._meteor_mode:
            # Meteor detection is lowest priority — exit it to allow tuning
            self.exit_meteor_mode()
            log.info("Exited meteor mode to allow tuning")
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
    def meteor_mode(self):
        return self._meteor_mode

    @property
    def wefax_mode(self):
        return self._wefax_mode

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
        # Always stop the source to release the USB device
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

    def enter_wefax_mode(self, frequency_khz):
        """Pause normal scanning and dedicate SDR to WEFAX HF direct sampling."""
        if self.mode != "SDR":
            log.warning("WEFAX mode only supported in SDR mode")
            return False
        if self._apt_mode:
            log.warning("Cannot enter WEFAX mode — APT satellite pass has priority")
            return False
        if self._wefax_mode:
            log.warning("Already in WEFAX mode")
            return False

        self._wefax_saved_preset = self.current_preset
        # Always stop the source to release the USB device, even if not "running"
        self._source.stop()

        self._wefax_mode = True
        log.info("Entered WEFAX mode — SDR dedicated to %.1f kHz HF direct sampling", frequency_khz)

        if self._error_callback:
            self._error_callback("wefax_mode_entered", {
                "message": f"SDR in WEFAX recording mode ({frequency_khz:.1f} kHz)",
            })
        return True

    def exit_wefax_mode(self):
        """Exit WEFAX recording mode and resume normal scanning."""
        if not self._wefax_mode:
            return

        self._wefax_mode = False
        log.info("Exited WEFAX mode")

        # Resume previous preset if one was active
        if self._wefax_saved_preset:
            preset = self._wefax_saved_preset
            self._wefax_saved_preset = None
            self.tune(preset)
            log.info("Resumed scanning: %s", preset.get("label", ""))
        else:
            self._wefax_saved_preset = None

        if self._error_callback:
            self._error_callback("wefax_mode_exited", {
                "message": "WEFAX recording complete — normal scanning resumed",
            })

    def enter_meteor_mode(self, frequency_hz):
        """Enter meteor detection mode — lowest priority, preempted by everything."""
        if self.mode != "SDR":
            log.warning("Meteor mode only supported in SDR mode")
            return False
        if self._apt_mode or self._wefax_mode:
            log.warning("Cannot enter meteor mode — higher priority mode active")
            return False
        if self._meteor_mode:
            log.warning("Already in meteor mode")
            return False

        self._meteor_saved_preset = self.current_preset
        was_running = self.is_running
        if was_running:
            self._source.stop()

        self._meteor_mode = True
        log.info("Entered meteor mode — monitoring %.3f MHz", frequency_hz / 1e6)
        return True

    def exit_meteor_mode(self):
        """Exit meteor detection mode and resume normal scanning."""
        if not self._meteor_mode:
            return

        self._meteor_mode = False
        log.info("Exited meteor mode")

        if self._meteor_saved_preset:
            preset = self._meteor_saved_preset
            self._meteor_saved_preset = None
            self.tune(preset)
            log.info("Resumed scanning: %s", preset.get("label", ""))
        else:
            self._meteor_saved_preset = None
