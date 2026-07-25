# Hands Socket.IO events from real OS threads to eventlet's hub.
#
# The hazard this removes
# ----------------------
# Several subsystems must run on REAL threads because they do blocking reads on
# subprocess pipes (meteor detector, rtl_433 / acarsdec / multimon-ng decoders).
# Calling socketio.emit() directly from such a thread reaches into eventlet's
# green synchronisation primitives from a thread the hub does not own, which
# fails as:
#
#   greenlet.error: Cannot switch to a different thread
#
# ...taking the emitting subsystem down with it (observed: the meteor detector
# dying mid-detection with "rtl_fm stream ended unexpectedly" right after the
# greenlet error).
#
# So real threads never emit. They enqueue onto a REAL (non-green) queue, and one
# greenthread owned by the hub drains it and does the actual emitting.
#
# Note the asymmetry that makes this correct: the producer side must be a real
# thread-safe queue, but the consumer must NEVER call a blocking get() — that
# would block the hub, which is the very thing we are avoiding. The drain polls
# with get_nowait() and yields between passes.

import logging

# Real queue, not eventlet's green one: producers are OS threads.
try:
    from eventlet.patcher import original
    queue = original("queue")
except ImportError:
    import queue

log = logging.getLogger(__name__)

DEFAULT_MAX_QUEUE = 2000
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_BATCH = 200


class ThreadSafeEmitter:
    """Callable with the same shape as socketio.emit, safe from any thread.

    Use as a drop-in `emit_fn` for anything running on a real thread. A
    greenthread must run drain_forever() (or call drain_once() periodically) for
    queued events to reach clients.
    """

    def __init__(self, emit_fn, max_queue=DEFAULT_MAX_QUEUE):
        self._emit = emit_fn
        self._queue = queue.Queue(maxsize=max_queue)
        self._dropped = 0
        self._emitted = 0
        self._failed = 0

    # ── Producer side (any thread) ──

    def __call__(self, event, data=None, **kwargs):
        """Enqueue an event. Never blocks, never raises."""
        try:
            self._queue.put_nowait((event, data, kwargs))
        except queue.Full:
            # Shed the oldest rather than block a hardware thread or grow without
            # bound: for telemetry, the newest state is the useful one.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((event, data, kwargs))
            except queue.Empty:
                pass
            except queue.Full:
                pass
            self._dropped += 1
            if self._dropped % 100 == 1:
                log.warning("emit bridge saturated — dropped %d event(s); "
                            "clients may be slow or the hub is starved",
                            self._dropped)

    # ── Consumer side (greenthread only) ──

    def drain_once(self, max_items=DEFAULT_BATCH):
        """Emit up to max_items queued events. Returns how many were emitted."""
        sent = 0
        for _ in range(max_items):
            try:
                event, data, kwargs = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                if data is None and not kwargs:
                    self._emit(event)
                else:
                    self._emit(event, data, **kwargs)
                self._emitted += 1
                sent += 1
            except Exception:
                # A failed emit must not kill the drain loop or lose the rest.
                self._failed += 1
                log.exception("emit bridge failed to deliver %r", event)
        return sent

    def drain_forever(self, sleep_fn, poll_interval=DEFAULT_POLL_INTERVAL,
                      should_run=None):
        """Poll-and-emit loop. Run this in a greenthread, never a real thread."""
        should_run = should_run or (lambda: True)
        while should_run():
            try:
                self.drain_once()
            except Exception:
                log.exception("emit bridge drain pass failed")
            sleep_fn(poll_interval)

    # ── Diagnostics ──

    @property
    def stats(self):
        return {
            "queued": self._queue.qsize(),
            "emitted": self._emitted,
            "dropped": self._dropped,
            "failed": self._failed,
        }
