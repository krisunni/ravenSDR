# Serializes SDR switch requests and owns the observable SDR state.
#
# Why this exists
# ---------------
# Switching the dongle is SLOW (~1-2s): the current rtl_fm must be SIGTERMed,
# the kernel has to release the USB interface, then a new process opens it. HTTP
# tune requests arrive far faster than that — a user clicking through presets
# generates five requests in a few seconds. Applying those concurrently races on
# the dongle: overlapping stop/start paths interleave, one request's cleanup
# clobbers another's freshly-spawned process, and the orphan left behind holds
# the device so every later tune / APT capture / piped decoder fails with
# usb_claim_interface -6 ("device busy").
#
# So switches are queued and applied by exactly ONE worker, never concurrently.
#
# Requests coalesce: while a switch is in flight, a newer request replaces any
# older pending one rather than being appended. Only the final target matters —
# applying every intermediate preset in turn would make the UI lag seconds
# behind the last click for no benefit.
#
# The arbiter is also the single source of truth for what the SDR is doing, so
# the UI can render current state, the expected (target) state, and the
# transition between them instead of guessing.

import logging
import time

log = logging.getLogger(__name__)

# Arbiter states, in command-and-control terms: the SDR is a separate piece of
# hardware that is commanded to a state and reports back its actual state.
LOCKED = "LOCKED"        # actual == commanded, nothing pending
SWITCHING = "SWITCHING"  # actual != commanded, a switch is being applied
FAULT = "FAULT"          # last commanded switch failed; actual is stale


class SdrArbiter:
    """Queue of one: serialize SDR switches, latest request wins.

    apply_fn(preset) does the real work and returns (ok, error_message).
    It is only ever called from the worker, one call at a time.
    """

    def __init__(self, apply_fn, on_change=None, on_error=None,
                 sleep_fn=None, poll_interval=0.05):
        self._apply_fn = apply_fn
        self._on_change = on_change or (lambda snapshot: None)
        self._on_error = on_error or (lambda message, preset: None)
        self._sleep = sleep_fn or time.sleep
        self._poll_interval = poll_interval

        self._actual = None       # state the hardware is confirmed to be in
        self._pending = None      # commanded state, not yet being applied
        self._in_flight = None    # commanded state being applied right now
        self._state = LOCKED
        self._running = False
        self._superseded = 0      # commands dropped by coalescing (diagnostics)
        self._last_error = None
        self._settled_at = time.time()

    # ── Observable state ──

    @property
    def actual(self):
        """What the hardware is confirmed to be doing."""
        return self._actual

    @property
    def commanded(self):
        """What the hardware has been told to do (in-flight, else pending)."""
        return self._in_flight or self._pending or self._actual

    @property
    def state(self):
        return self._state

    @property
    def is_switching(self):
        return self._state == SWITCHING

    def snapshot(self):
        """Serializable C2 view: commanded vs actual, plus the transition."""
        return {
            "state": self._state,
            "actual": _brief(self._actual),
            "commanded": _brief(self.commanded),
            # True while the hardware is not yet where it was commanded to be.
            "in_transition": self._state == SWITCHING or self._pending is not None,
            "pending": self._pending is not None,
            "superseded": self._superseded,
            "last_error": self._last_error,
            "settled_for": round(max(0.0, time.time() - self._settled_at), 1),
        }

    def adopt(self, preset):
        """Record hardware state that was set outside the arbiter.

        Startup auto-tune and /api/stop drive the SDR directly rather than
        issuing a command. Without telling the arbiter, it would report
        actual=None while the radio was in fact tuned, and the console's ACTUAL
        field would sit blank until the first operator command.
        """
        self._actual = preset
        if self._pending is None and self._in_flight is None:
            self._state = LOCKED
            self._last_error = None
            self._settled_at = time.time()
        self._on_change(self.snapshot())

    # ── Requesting a switch ──

    def request(self, preset):
        """Command the SDR to a new state. Returns the snapshot after queueing.

        Never blocks on the hardware — the caller (an HTTP handler) returns
        immediately and the UI follows the transition over Socket.IO.
        """
        if self._pending is not None:
            # Coalesce: the older commanded state is now irrelevant.
            self._superseded += 1
            log.info("Superseding commanded SDR state %s -> %s",
                     _label(self._pending), _label(preset))
        self._pending = preset
        self._last_error = None
        self._state = SWITCHING
        snap = self.snapshot()
        self._on_change(snap)
        return snap

    # ── Worker ──

    def start(self, spawn_fn):
        """Begin processing requests. spawn_fn runs the worker concurrently."""
        if self._running:
            return
        self._running = True
        spawn_fn(self._worker_loop)

    def stop(self):
        self._running = False

    def _worker_loop(self):
        while self._running:
            preset = self._take_pending()
            if preset is None:
                self._sleep(self._poll_interval)
                continue
            self._apply_one(preset)

    def _take_pending(self):
        """Claim the pending request, if any. Single-worker, so no lock needed."""
        preset = self._pending
        if preset is None:
            return None
        self._pending = None
        self._in_flight = preset
        self._state = SWITCHING
        return preset

    def _apply_one(self, preset):
        started = time.time()
        ok, error = False, None
        try:
            ok, error = self._apply_fn(preset)
        except Exception as e:                      # never kill the worker
            ok, error = False, f"{type(e).__name__}: {e}"
            log.exception("SDR switch to %s raised", _label(preset))

        self._in_flight = None
        elapsed = time.time() - started

        if ok:
            self._actual = preset
            self._last_error = None
            log.info("SDR LOCKED on %s in %.2fs", _label(preset), elapsed)
        else:
            self._last_error = error or "switch failed"
            log.error("SDR switch to %s FAULTED after %.2fs: %s",
                      _label(preset), elapsed, self._last_error)
            self._on_error(self._last_error, preset)

        # Only settle if nothing arrived while we were busy; otherwise stay
        # SWITCHING so the UI never flickers "locked" between coalesced clicks.
        if self._pending is None:
            self._state = LOCKED if ok else FAULT
            self._settled_at = time.time()
        self._on_change(self.snapshot())


def _brief(preset):
    """Minimal preset view for status payloads."""
    if not preset:
        return None
    return {
        "id": preset.get("id"),
        "label": preset.get("label"),
        "freq": preset.get("freq"),
        "mode": preset.get("mode"),
        "category": preset.get("category"),
        # True while the background corpus collector holds the dongle, so the
        # console can explain an idle audio path instead of leaving it blank.
        "collecting": bool(preset.get("collecting")),
    }


def _label(preset):
    if not preset:
        return "None"
    return preset.get("id") or preset.get("label") or "?"
