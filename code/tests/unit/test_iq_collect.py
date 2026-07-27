"""Unit tests for raw IQ capture and the background collection rotation.

Exists because the IQ pipeline never ran: pyrtlsdr cannot load against the
RTL-SDR Blog driver ("undefined symbol: rtlsdr_set_dithering") and the tuner's
rtl_fm path emits demodulated audio, not IQ. rtl_sdr(1) is the way in.
"""

import numpy as np
import pytest

from ravensdr.iq_collect_scheduler import IQCollectScheduler
from ravensdr.iq_collector import IQCollector, bytes_to_iq


class TestBytesToIQ:
    def test_centres_unsigned_bytes(self):
        """rtl_sdr emits unsigned 8-bit centred on 127.5."""
        iq = bytes_to_iq(bytes([128, 128, 128, 128]))
        assert np.allclose(iq, [0.5 + 0.5j, 0.5 + 0.5j])

    def test_interleaving_is_i_then_q(self):
        iq = bytes_to_iq(bytes([255, 0]))
        assert iq[0].real > 0 and iq[0].imag < 0

    def test_odd_trailing_byte_is_dropped(self):
        """A split I/Q pair across reads must not skew the stream."""
        iq = bytes_to_iq(bytes([128, 128, 200]))
        assert len(iq) == 1

    def test_empty_and_single_byte(self):
        assert len(bytes_to_iq(b"")) == 0
        assert len(bytes_to_iq(bytes([128]))) == 0

    def test_dtype_is_complex64(self):
        assert bytes_to_iq(bytes([1, 2, 3, 4])).dtype == np.complex64

    def test_real_capture_shape(self):
        raw = bytes(np.random.randint(0, 256, 2048, dtype=np.uint8))
        assert len(bytes_to_iq(raw)) == 1024


class TestCollectorCommand:
    def test_command_carries_frequency_rate_and_device(self):
        c = IQCollector(device_index=1, sample_rate=2400000)
        cmd = c.build_cmd(94_900_000)
        assert cmd[0] == "rtl_sdr"
        assert "94900000" in cmd and "2400000" in cmd
        assert cmd[cmd.index("-d") + 1] == "1"
        assert cmd[-1] == "-", "must stream to stdout"

    def test_gain_included_only_when_set(self):
        assert "-g" not in IQCollector().build_cmd(1)
        assert "-g" in IQCollector(gain=40).build_cmd(1)

    def test_missing_binary_is_survivable(self, monkeypatch):
        import ravensdr.iq_collector as mod
        monkeypatch.setattr(mod.subprocess, "Popen",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        assert IQCollector().start(1) is False


def _bands():
    return [
        {"id": "fm", "freq_hz": 94_900_000, "label": "WFM"},
        {"id": "air", "freq_hz": 131_550_000, "label": "AM"},
    ]


class _Harness:
    """Drives the scheduler synchronously with a fake clock."""

    def __init__(self, enabled=True):
        self.started = []
        self.stopped = []
        self.enabled = enabled
        self.ticks = 0

    def start_slot(self, band):
        self.started.append(band["id"])
        return True

    def stop_slot(self, band):
        self.stopped.append(band["id"])

    def is_enabled(self):
        return self.enabled

    def sleep(self, _seconds):
        self.ticks += 1


class TestRotation:
    def _sched(self, h, **kw):
        return IQCollectScheduler(
            _bands(), h.start_slot, h.stop_slot, h.is_enabled,
            dwell_s=kw.pop("dwell_s", 5), idle_s=kw.pop("idle_s", 0),
            sleep_fn=h.sleep, **kw)

    def test_visits_each_band_in_turn(self):
        h = _Harness()
        s = self._sched(h)
        s._running = True
        s._run_slot(s.bands[0])
        s._run_slot(s.bands[1])
        assert h.started == ["fm", "air"]

    def test_each_slot_releases_the_dongle(self):
        """A slot that never releases would strand the radio in collect mode."""
        h = _Harness()
        s = self._sched(h)
        s._running = True
        s._run_slot(s.bands[0])
        assert h.stopped == ["fm"]

    def test_slot_dwells_before_releasing(self):
        h = _Harness()
        s = self._sched(h, dwell_s=7)
        s._running = True
        s._run_slot(s.bands[0])
        assert h.ticks == 7

    def test_disabled_mid_slot_stops_early(self):
        """Pausing automation must not require waiting out the dwell."""
        h = _Harness()
        s = self._sched(h, dwell_s=100)

        def sleep(_):
            h.ticks += 1
            if h.ticks >= 3:
                h.enabled = False

        s._sleep = sleep
        s._running = True
        s._run_slot(s.bands[0])
        assert h.ticks < 100
        assert h.stopped == ["fm"]

    def test_failed_start_does_not_claim_a_slot(self):
        h = _Harness()
        h.start_slot = lambda band: False
        s = self._sched(h)
        s._running = True
        s._run_slot(s.bands[0])
        assert h.stopped == []
        assert s.snapshot()["last_error"]

    def test_raising_start_is_contained(self):
        h = _Harness()
        h.start_slot = lambda band: (_ for _ in ()).throw(RuntimeError("busy"))
        s = self._sched(h)
        s._running = True
        s._run_slot(s.bands[0])       # must not propagate
        assert "RuntimeError" in s.snapshot()["last_error"]

    def test_stop_releases_a_live_slot(self):
        h = _Harness()
        s = self._sched(h)
        s._current = s.bands[0]
        s.stop()
        assert h.stopped == ["fm"]
        assert s.snapshot()["running"] is False

    def test_snapshot_reports_gating_state(self):
        h = _Harness(enabled=False)
        s = self._sched(h)
        snap = s.snapshot()
        assert snap["enabled"] is False
        assert snap["bands"] == ["fm", "air"]

    def test_start_requires_bands(self):
        h = _Harness()
        s = IQCollectScheduler([], h.start_slot, h.stop_slot, h.is_enabled,
                               sleep_fn=h.sleep)
        assert s.start(lambda fn: None) is False

    def test_dwell_has_a_floor(self):
        h = _Harness()
        s = self._sched(h, dwell_s=0)
        assert s.dwell_s >= 5
