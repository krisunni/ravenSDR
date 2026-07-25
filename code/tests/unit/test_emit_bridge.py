"""Unit tests for ThreadSafeEmitter — the real-thread -> eventlet-hub bridge.

Exists because emitting Socket.IO events directly from a real OS thread raises
`greenlet.error: Cannot switch to a different thread` and killed the meteor
detector mid-detection.
"""

import threading

from ravensdr.emit_bridge import ThreadSafeEmitter


class _Sink:
    def __init__(self, fail_on=None):
        self.calls = []
        self._fail_on = fail_on or set()

    def __call__(self, event, data=None, **kwargs):
        if event in self._fail_on:
            raise RuntimeError("emit exploded")
        self.calls.append((event, data, kwargs))


class TestQueueing:
    def test_call_does_not_emit_immediately(self):
        """The producing thread must never touch the hub."""
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink)
        bridge("meteor_detection", {"id": 1})
        assert sink.calls == []
        assert bridge.stats["queued"] == 1

    def test_drain_emits_queued_events_in_order(self):
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink)
        for i in range(3):
            bridge("ev", {"i": i})
        assert bridge.drain_once() == 3
        assert [c[1]["i"] for c in sink.calls] == [0, 1, 2]
        assert bridge.stats["queued"] == 0

    def test_event_with_no_payload(self):
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink)
        bridge("ping")
        bridge.drain_once()
        assert sink.calls == [("ping", None, {})]

    def test_kwargs_are_forwarded(self):
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink)
        bridge("ev", {"a": 1}, room="ops")
        bridge.drain_once()
        assert sink.calls[0][2] == {"room": "ops"}

    def test_drain_on_empty_queue_is_zero(self):
        assert ThreadSafeEmitter(_Sink()).drain_once() == 0

    def test_drain_respects_batch_limit(self):
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink)
        for i in range(10):
            bridge("ev", {"i": i})
        assert bridge.drain_once(max_items=4) == 4
        assert bridge.stats["queued"] == 6


class TestOverflow:
    def test_saturated_queue_sheds_oldest_and_keeps_newest(self):
        """A hardware thread must never block on a slow client."""
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink, max_queue=3)
        for i in range(6):
            bridge("ev", {"i": i})
        bridge.drain_once()
        seen = [c[1]["i"] for c in sink.calls]
        assert len(seen) == 3
        assert 5 in seen              # newest survived
        assert 0 not in seen          # oldest shed
        assert bridge.stats["dropped"] == 3

    def test_producer_never_raises_when_full(self):
        bridge = ThreadSafeEmitter(_Sink(), max_queue=1)
        for i in range(50):
            bridge("ev", {"i": i})    # must not raise


class TestResilience:
    def test_failed_emit_does_not_lose_later_events(self):
        sink = _Sink(fail_on={"bad"})
        bridge = ThreadSafeEmitter(sink)
        bridge("bad", {})
        bridge("good", {})
        bridge.drain_once()
        assert [c[0] for c in sink.calls] == ["good"]
        assert bridge.stats["failed"] == 1

    def test_drain_forever_stops_when_should_run_false(self):
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink)
        bridge("ev", {})
        passes = {"n": 0}

        def should_run():
            passes["n"] += 1
            return passes["n"] <= 2

        bridge.drain_forever(sleep_fn=lambda s: None, should_run=should_run)
        assert sink.calls           # drained before stopping


class TestThreadSafety:
    def test_many_real_threads_can_enqueue_concurrently(self):
        sink = _Sink()
        bridge = ThreadSafeEmitter(sink, max_queue=10000)

        def produce(n):
            for i in range(100):
                bridge("ev", {"t": n, "i": i})

        threads = [threading.Thread(target=produce, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = 0
        while True:
            sent = bridge.drain_once()
            total += sent
            if sent == 0:
                break
        assert total == 800
        assert bridge.stats["dropped"] == 0
