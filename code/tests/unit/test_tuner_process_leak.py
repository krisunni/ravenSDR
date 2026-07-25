"""Regression tests for rtl_fm process lifecycle in Tuner.

Covers the orphaned-rtl_fm bug: two overlapping tune() calls could interleave so
that one greenthread cleared self._pid after another had already stored a fresh
pid, leaving an rtl_fm alive forever. That orphan held the RTL-SDR, so every
later tune, APT capture and piped decoder (pager/ISM/ACARS) failed with
usb_claim_interface -6.
"""

import pytest

from ravensdr import tuner as tuner_mod
from ravensdr.tuner import Tuner


class _FakeProc:
    """Stand-in for a Popen object with closable pipes."""

    def __init__(self, pid):
        self.pid = pid
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self._rc = None

    def poll(self):
        return self._rc


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def rtlfm_tuner(monkeypatch):
    """A Tuner forced onto the rtl_fm path, with kills recorded not performed."""
    monkeypatch.setattr(tuner_mod, "_check_pyrtlsdr", lambda: False)
    t = Tuner(pcm_queue=None, audio_queue=None)

    killed = []
    monkeypatch.setattr(tuner_mod, "_kill_pid", lambda pid: killed.append(pid))
    # Threads/queues are irrelevant to pid bookkeeping.
    monkeypatch.setattr(t, "_drain_queues", lambda: None)
    return t, killed


class TestSpawnedPidTracking:
    def test_stop_kills_the_current_pid(self, rtlfm_tuner):
        t, killed = rtlfm_tuner
        t._process = _FakeProc(101)
        t._pid = 101
        t._spawned_pids.add(101)

        t._stop_rtlfm()

        assert killed == [101]
        assert t._spawned_pids == set()
        assert t._pid is None

    def test_stop_reaps_orphan_clobbered_by_interleaved_tune(self, rtlfm_tuner):
        """The actual production failure: a pid was spawned but self._pid got
        overwritten, so the old code never killed it."""
        t, killed = rtlfm_tuner
        # 12342 was spawned, then a racing tune replaced _pid with 12545.
        t._spawned_pids.update({12342, 12545})
        t._process = _FakeProc(12545)
        t._pid = 12545

        t._stop_rtlfm()

        # Both must die — the orphan is what held the dongle.
        assert set(killed) == {12342, 12545}
        assert t._spawned_pids == set()

    def test_stop_is_safe_with_no_process(self, rtlfm_tuner):
        t, killed = rtlfm_tuner
        t._stop_rtlfm()
        assert killed == []
        assert t._is_running is False

    def test_stop_closes_pipes(self, rtlfm_tuner):
        t, killed = rtlfm_tuner
        proc = _FakeProc(7)
        t._process = proc
        t._pid = 7
        t._spawned_pids.add(7)

        t._stop_rtlfm()

        assert proc.stdout.closed and proc.stderr.closed

    def test_tune_records_spawned_pid(self, rtlfm_tuner, monkeypatch):
        t, killed = rtlfm_tuner
        monkeypatch.setattr(tuner_mod.subprocess, "Popen",
                            lambda *a, **k: _FakeProc(555))

        class _NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(tuner_mod.threading, "Thread", _NoThread)

        t._tune_rtlfm("146.960M", "fm")

        assert 555 in t._spawned_pids
        assert t._pid == 555

    def test_second_tune_kills_first_pid(self, rtlfm_tuner, monkeypatch):
        """Back-to-back tunes must leave exactly one live rtl_fm."""
        t, killed = rtlfm_tuner
        pids = iter([111, 222])
        monkeypatch.setattr(tuner_mod.subprocess, "Popen",
                            lambda *a, **k: _FakeProc(next(pids)))

        class _NoThread:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def join(self, timeout=None):
                pass

        monkeypatch.setattr(tuner_mod.threading, "Thread", _NoThread)

        t._tune_rtlfm("146.960M", "fm")
        t._tune_rtlfm("162.550M", "fm")

        assert 111 in killed
        assert t._pid == 222
        assert t._spawned_pids == {222}


class TestStopOrderingAvoidsDeadlock:
    """Regression: closing rtl_fm's pipes before killing it froze the whole app.

    _read_loop blocks in stdout.read() holding BufferedReader's internal lock. A
    close() from the eventlet hub's thread then waits on that lock forever when
    no bytes are coming — which is exactly what a squelched preset on a quiet
    channel produces. Killing first makes read() return EOF and release it.
    """

    def test_process_is_killed_before_pipes_are_closed(self, rtlfm_tuner,
                                                       monkeypatch):
        t, killed = rtlfm_tuner
        events = []

        class _OrderedPipe:
            def close(self):
                events.append("close")

        proc = _FakeProc(42)
        proc.stdout = _OrderedPipe()
        proc.stderr = _OrderedPipe()
        t._process = proc
        t._pid = 42
        t._spawned_pids.add(42)

        monkeypatch.setattr(tuner_mod, "_kill_pid",
                            lambda pid: events.append("kill"))

        t._stop_rtlfm()

        assert events[0] == "kill", f"pipes closed before kill: {events}"
        assert events.count("close") == 2

    def test_orphans_are_still_all_killed_with_new_ordering(self, rtlfm_tuner):
        t, killed = rtlfm_tuner
        t._spawned_pids.update({100, 200})
        t._process = _FakeProc(200)
        t._pid = 200
        t._stop_rtlfm()
        assert set(killed) == {100, 200}
