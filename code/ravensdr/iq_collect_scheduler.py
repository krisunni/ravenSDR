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
                 sleep_fn=None, on_change=None, dwell_fn=None, should_yield=None):
        """
        bands       — list of {"id", "freq_hz", "label"} to rotate through
        start_slot  — fn(band) -> bool, takes the dongle and begins capture
        stop_slot   — fn(band), releases the dongle
        is_enabled  — fn() -> bool, consulted before EVERY slot
        """
        # Group by modulation, and visit one frequency PER CLASS per rotation.
        #
        # A flat list gives each frequency equal airtime, which sounds fair but
        # is not: FM is carried by 17 presets and APRS by one, so a flat rotation
        # collected FM 17x faster and drove the corpus to a 20.9x imbalance
        # (FM 1191 vs AFSK1200 57). Frequency diversity within a class is still
        # wanted — it stops the model learning the band instead of the modulation
        # — so the frequency representing each class advances every rotation.
        self.bands = list(bands)
        self._groups = {}
        for b in self.bands:
            self._groups.setdefault(b.get("label"), []).append(b)
        self._labels = sorted(self._groups)
        self._cursor = {lab: 0 for lab in self._labels}
        self._start_slot = start_slot
        # Predicate: True when the radio should be handed back immediately.
        self._should_yield = should_yield or (lambda: False)
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
        # Set by preempt() when something with a deadline needs the radio NOW.
        # Distinct from _should_yield, which only reports operator activity —
        # a satellite AOS cannot wait for the next poll of that.
        self._preempted = False
        self._index = 0
        self._current = None
        self._rotations = 0
        self._slots_run = 0
        self._last_error = None

    # ── Observable state ──

    def preempt(self):
        """Give the radio up now, for something that has a deadline.

        Nothing could do this before. _start_slot refuses to BEGIN a slot while
        APT/WEFAX holds the radio, but an already-running slot was invisible to
        every preemption path, and a dwell can stretch to 240s. A pass firing
        inside one found rtl_sdr still on the dongle: the journal shows "APT WAV
        not created / empty (device busy)" landing ~23ms after "recording
        started" for 11 of 22 passes, including a 68-degree NOAA-15 pass lost to
        a 345 MHz corpus dwell.

        Returns True if a slot was actually running, so the caller knows whether
        it has to wait for the device to come free.
        """
        was_running = self._current is not None
        self._preempted = True
        return was_running

    def snapshot(self):
        return {
            "running": self._running,
            "enabled": bool(self._is_enabled()),
            "current_band": self._current,
            "bands": [b["id"] for b in self.bands],
            "classes": self._labels,
            "frequencies_per_class": {k: len(v) for k, v in self._groups.items()},
            "dwell_s": self.dwell_s,
            "idle_s": self.idle_s,
            "rotations": self._rotations,
            "slots_run": self._slots_run,
            "last_error": self._last_error,
        }

    # ── Lifecycle ──

    def start(self, spawn_fn):
        if self._running or not self._labels:
            return False
        self._running = True
        spawn_fn(self._loop)
        log.info("IQ collect scheduler started (%d classes over %d frequencies, "
                 "%ds dwell)", len(self._labels), len(self.bands), self.dwell_s)
        return True

    def stop(self):
        self._running = False
        self._release()
        self._preempted = False

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

            label = self._labels[self._index]
            group = self._groups[label]
            # Next frequency for this class, advancing each time we return to it.
            band = group[self._cursor[label] % len(group)]
            self._cursor[label] += 1

            self._index = (self._index + 1) % len(self._labels)
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
            if self._preempted:
                log.info("IQ collect: preempted mid-slot — releasing %s",
                         band.get("id"))
                break
            if self._should_yield():
                # Somebody tuned. Corpus building is opportunistic and has no
                # deadline, so it gives the radio back inside a second rather
                # than making a person wait out a dwell that can run to 4 min.
                log.info("IQ collect: yielding %s — operator took the radio",
                         band.get("id"))
                break
            self._sleep(1)
            waited += 1

        self._release()
