# One Hailo VDevice for the whole process.
#
# The Hailo-8L is a single physical device. ROUND_ROBIN scheduling lets several
# network groups share it — but only ones configured on the SAME VDevice. Two
# VDevice objects contend for the hardware and whichever asks second gets
# HAILO_OUT_OF_PHYSICAL_DEVICES (74).
#
# Three components want the NPU here: Whisper, the signal classifier and SEI.
# Each used to create its own VDevice, which worked only because just one of
# them ever had a .hef to load. The moment the classifier got one, it took the
# device at startup and Whisper — the more valuable tenant by far — silently
# fell back to CPU. The error was logged, the console still said "hailo" for the
# classifier, and transcription just got slow.
#
# Handing everyone the same VDevice lets the scheduler do what it is for.

import logging
import threading

log = logging.getLogger(__name__)

_lock = threading.Lock()
_vdevice = None
_failed = False


def get_vdevice():
    """Return the process-wide VDevice, creating it on first use.

    Returns None if the device cannot be opened, so callers fall back to CPU
    rather than raising. Never call close() on the result — it is shared.
    """
    global _vdevice, _failed
    with _lock:
        if _vdevice is not None or _failed:
            return _vdevice
        try:
            from hailo_platform import VDevice, HailoSchedulingAlgorithm
            params = VDevice.create_params()
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            _vdevice = VDevice(params)
            log.info("Hailo VDevice opened (shared, ROUND_ROBIN scheduling)")
        except Exception as e:
            _failed = True
            log.warning("Hailo VDevice unavailable (%s) — CPU fallbacks apply", e)
        return _vdevice


def is_available():
    return get_vdevice() is not None
