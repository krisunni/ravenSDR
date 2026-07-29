"""Unit tests for SdrArbiter — the serialized SDR command queue.

The arbiter exists because switching the dongle takes ~1-2s while HTTP tune
requests arrive in milliseconds. Applying them concurrently raced on the device
and orphaned rtl_fm processes that then held it ("device busy").
"""

from ravensdr.sdr_arbiter import FAULT, LOCKED, SWITCHING, SdrArbiter


def _preset(pid, **kw):
    base = {"id": pid, "label": pid.upper(), "freq": "146.960M",
            "mode": "fm", "category": "ham"}
    base.update(kw)
    return base


class _Recorder:
    """Records apply_fn calls and lets a test decide each outcome."""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])
        self.concurrent = 0
        self.max_concurrent = 0

    def __call__(self, preset):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.calls.append(preset["id"])
        try:
            if self.results:
                return self.results.pop(0)
            return True, None
        finally:
            self.concurrent -= 1


def _drain(arb, rounds=50):
    """Run the worker body synchronously until nothing is pending."""
    for _ in range(rounds):
        preset = arb._take_pending()
        if preset is None:
            return
        arb._apply_one(preset)


class TestInitialState:
    def test_starts_locked_with_nothing_commanded(self):
        arb = SdrArbiter(apply_fn=_Recorder())
        snap = arb.snapshot()
        assert snap["state"] == LOCKED
        assert snap["actual"] is None
        assert snap["commanded"] is None
        assert snap["in_transition"] is False


class TestRequestAndApply:
    def test_request_marks_switching_before_hardware_moves(self):
        rec = _Recorder()
        arb = SdrArbiter(apply_fn=rec)
        snap = arb.request(_preset("a"))
        # Commanded immediately; actual still unset until the worker runs.
        assert snap["state"] == SWITCHING
        assert snap["commanded"]["id"] == "a"
        assert snap["actual"] is None
        assert snap["in_transition"] is True
        assert rec.calls == []      # request() must not touch hardware

    def test_worker_applies_and_locks(self):
        rec = _Recorder()
        arb = SdrArbiter(apply_fn=rec)
        arb.request(_preset("a"))
        _drain(arb)
        snap = arb.snapshot()
        assert rec.calls == ["a"]
        assert snap["state"] == LOCKED
        assert snap["actual"]["id"] == "a"
        assert snap["commanded"]["id"] == "a"
        assert snap["in_transition"] is False

    def test_snapshot_exposes_only_brief_preset_fields(self):
        arb = SdrArbiter(apply_fn=_Recorder())
        arb.request(_preset("a", note="secret", stream_url="http://x"))
        _drain(arb)
        actual = arb.snapshot()["actual"]
        assert set(actual) == {
            "id", "label", "freq", "mode", "category", "collecting"}
        # The point of the brief view: verbose or sensitive preset fields must
        # not ride along into every status payload.
        assert "note" not in actual
        assert "stream_url" not in actual

    def test_collecting_defaults_false_for_operator_presets(self):
        """Only the corpus collector sets this; a normal tune must not."""
        arb = SdrArbiter(apply_fn=_Recorder())
        arb.request(_preset("a"))
        _drain(arb)
        assert arb.snapshot()["actual"]["collecting"] is False

    def test_adopt_carries_the_collecting_flag(self):
        """The collector takes the dongle outside the arbiter and reports it
        via adopt(); without the flag the console shows the operator's preset
        as live while the radio is off building the corpus elsewhere."""
        arb = SdrArbiter(apply_fn=_Recorder())
        arb.adopt({"id": "aprs-144390", "label": "aprs-144390 (collecting "
                   "AFSK1200)", "freq": "144.3900M", "mode": "iq-collect",
                   "collecting": True})
        snap = arb.snapshot()
        assert snap["actual"]["collecting"] is True
        assert snap["actual"]["id"] == "aprs-144390"


class TestCoalescing:
    def test_rapid_commands_coalesce_to_the_last(self):
        """Five clicks must move the hardware once, to the final target."""
        rec = _Recorder()
        arb = SdrArbiter(apply_fn=rec)
        for pid in ("a", "b", "c", "d", "e"):
            arb.request(_preset(pid))
        _drain(arb)
        assert rec.calls == ["e"]
        assert arb.snapshot()["actual"]["id"] == "e"
        assert arb.snapshot()["superseded"] == 4

    def test_command_arriving_during_apply_is_applied_next(self):
        rec = _Recorder()
        arb = SdrArbiter(apply_fn=rec)
        arb.request(_preset("a"))
        preset = arb._take_pending()
        arb.request(_preset("b"))       # arrives mid-flight
        arb._apply_one(preset)
        # Still switching: 'b' is outstanding, so we must not flash LOCKED.
        assert arb.snapshot()["state"] == SWITCHING
        assert arb.snapshot()["actual"]["id"] == "a"
        assert arb.snapshot()["commanded"]["id"] == "b"
        _drain(arb)
        assert rec.calls == ["a", "b"]
        assert arb.snapshot()["state"] == LOCKED

    def test_commanded_reflects_in_flight_target(self):
        arb = SdrArbiter(apply_fn=_Recorder())
        arb.request(_preset("a"))
        arb._take_pending()
        assert arb.snapshot()["commanded"]["id"] == "a"


class TestSerialization:
    def test_apply_never_overlaps(self):
        rec = _Recorder()
        arb = SdrArbiter(apply_fn=rec)
        for pid in ("a", "b"):
            arb.request(_preset(pid))
            _drain(arb)
        assert rec.max_concurrent == 1


class TestFailureHandling:
    def test_failed_switch_faults_and_keeps_last_actual(self):
        rec = _Recorder(results=[(True, None), (False, "device busy")])
        errors = []
        arb = SdrArbiter(apply_fn=rec, on_error=lambda m, p: errors.append((m, p["id"])))
        arb.request(_preset("a"))
        _drain(arb)
        arb.request(_preset("b"))
        _drain(arb)
        snap = arb.snapshot()
        assert snap["state"] == FAULT
        assert snap["last_error"] == "device busy"
        # Actual stays on the last confirmed state — 'b' never took effect.
        assert snap["actual"]["id"] == "a"
        assert errors == [("device busy", "b")]

    def test_exception_in_apply_faults_without_killing_worker(self):
        def boom(preset):
            raise RuntimeError("usb_claim_interface -6")

        arb = SdrArbiter(apply_fn=boom)
        arb.request(_preset("a"))
        _drain(arb)
        snap = arb.snapshot()
        assert snap["state"] == FAULT
        assert "usb_claim_interface -6" in snap["last_error"]

    def test_recovers_to_locked_after_a_fault(self):
        rec = _Recorder(results=[(False, "busy"), (True, None)])
        arb = SdrArbiter(apply_fn=rec)
        arb.request(_preset("a"))
        _drain(arb)
        assert arb.snapshot()["state"] == FAULT
        arb.request(_preset("b"))
        _drain(arb)
        assert arb.snapshot()["state"] == LOCKED
        assert arb.snapshot()["last_error"] is None

    def test_new_command_clears_stale_error(self):
        rec = _Recorder(results=[(False, "busy")])
        arb = SdrArbiter(apply_fn=rec)
        arb.request(_preset("a"))
        _drain(arb)
        snap = arb.request(_preset("b"))
        assert snap["last_error"] is None


class TestNotifications:
    def test_on_change_fires_for_command_and_settle(self):
        states = []
        arb = SdrArbiter(apply_fn=_Recorder(),
                         on_change=lambda s: states.append(s["state"]))
        arb.request(_preset("a"))
        _drain(arb)
        assert states[0] == SWITCHING
        assert states[-1] == LOCKED
