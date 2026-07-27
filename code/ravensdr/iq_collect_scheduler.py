# Background rotation that builds a labelled IQ training corpus.
#
# Cycles through a band list, dwelling on each for a fixed period, capturing raw
# IQ and handing detected transmissions to a labelled-sample collector. The
# preset's declared modulation is the label — see signal_classifier.collect_sample
# for why the classifier's own opinion cannot be used to bootstrap itself.
#
# This SEIZES THE DONGLE, so it is automation in the sense config.py means: it
# is gated behind the automation switch (task "iq_collect") and is off by
# default. While a slot is running the radio produces no audio for that band.
# One dongle cannot demodulate audio and stream IQ at the same time.

import logging
import time

log = logging.getLogger(__name__)

DEFAULT_DWELL_S = 60           # per band
DEFAULT_IDLE_S = 300           # rest between full rotations
MIN_DWELL_S = 5


class IQCollectScheduler:
    """Rotates IQ collection across bands while automation permits it."""

    def __init__(self, bands, start_slot, stop_slot, is_enabled,
                 dwell_s=DEFAULT_DWELL_S, idle_s=DEFAULT_IDLE_S,
                 sleep_fn=None, on_change=None, dwell_fn=None):
        """
        bands       — list of {"id", "freq_hz", "label"} to rotate through
        start_slot  — fn(band) -> bool, takes the dongle and begins capture
        stop_slot   — fn(band), releases the dongle
        is_enabled  — fn() -> bool, consulted before EVERY slot
        """
        self.bands = list(bands)
        self._start_slot = start_slot
        self._stop_slot = stop_slot
        self._is_enabled = is_enabled
        self.dwell_s = max(MIN_DWELL_S, dwell_s)
        self.idle_s = idle_s
        self._sleep = sleep_fn or time.sleep
        self._on_change = on_change or (lambda snapshot: None)
        # Optional per-band dwell. Equal dwell starves bursty channels: a
        # continuous carrier yields a sample every couple of seconds while an
        # idle one yields only when somebody transmits, so equal time produces a
        # ~10:1 class imbalance and a classifier that mostly predicts the
        # majority class.
        self._dwell_fn = dwell_fn

        self._running = False
        self._index = 0
        self._current = None
        self._rotations = 0
        self._slots_run = 0
        self._last_error = None

    # ── Observable state ──

    def snapshot(self):
        return {
            "running": self._running,
            "enabled": bool(self._is_enabled()),
            "current_band": self._current,
            "bands": [b["id"] for b in self.bands],
            "dwell_s": self.dwell_s,
            "idle_s": self.idle_s,
            "rotations": self._rotations,
            "slots_run": self._slots_run,
            "last_error": self._last_error,
        }

    # ── Lifecycle ──

    def start(self, spawn_fn):
        if self._running or not self.bands:
            return False
        self._running = True
        spawn_fn(self._loop)
        log.info("IQ collect scheduler started (%d bands, %ds dwell)",
                 len(self.bands), self.dwell_s)
        return True

    def stop(self):
        self._running = False
        self._release()

    def _release(self):
        if self._current is not None:
            band, self._current = self._current, None
            try:
                self._stop_slot(band)
            except Exception:
                log.exception("Failed releasing IQ collect slot")
            self._on_change(self.snapshot())

    def _loop(self):
        while self._running:
            # Re-check every slot: the operator can pause automation at any
            # moment and must not have to wait out a whole rotation.
            if not self._is_enabled():
                self._release()
                self._sleep(5)
                continue

            band = self.bands[self._index]
            self._index = (self._index + 1) % len(self.bands)
            if self._index == 0:
                self._rotations += 1

            self._run_slot(band)

            if self._running and self._index == 0 and self.idle_s:
                # Give the radio back between rotations so the node is not
                # permanently unavailable for listening.
                self._sleep(self.idle_s)

    def _run_slot(self, band):
        try:
            started = self._start_slot(band)
        except Exception as e:
            self._last_error = f"{type(e).__name__}: {e}"
            log.exception("IQ collect slot failed to start: %s", band.get("id"))
            return
        if not started:
            self._last_error = f"could not start {band.get('id')}"
            self._sleep(5)
            return

        self._current = band
        self._slots_run += 1
        self._last_error = None
        self._on_change(self.snapshot())

        dwell = self.dwell_s
        if self._dwell_fn is not None:
            try:
                dwell = max(MIN_DWELL_S, int(self._dwell_fn(band)))
            except Exception:
                log.exception("dwell_fn failed; using default")

        waited = 0
        while self._running and waited < dwell:
            if not self._is_enabled():
                break               # operator paused mid-slot
            self._sleep(1)
            waited += 1

        self._release()
