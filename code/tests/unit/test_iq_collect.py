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


class TestAdaptiveDwell:
    """Equal dwell starves bursty channels into a ~10:1 class imbalance."""

    def _sched(self, h, dwell_fn):
        return IQCollectScheduler(
            _bands(), h.start_slot, h.stop_slot, h.is_enabled,
            dwell_s=10, idle_s=0, sleep_fn=h.sleep, dwell_fn=dwell_fn)

    def test_dwell_fn_overrides_the_default(self):
        h = _Harness()
        s = self._sched(h, lambda band: 8)   # above MIN_DWELL_S
        s._running = True
        s._run_slot(s.bands[0])
        assert h.ticks == 8

    def test_under_represented_band_gets_more_time(self):
        h = _Harness()
        weights = {"fm": 5, "air": 20}
        s = self._sched(h, lambda band: weights[band["id"]])
        s._running = True
        s._run_slot(s.bands[0])
        first = h.ticks
        s._run_slot(s.bands[1])
        assert h.ticks - first > first

    def test_dwell_never_drops_below_the_floor(self):
        h = _Harness()
        s = self._sched(h, lambda band: 0)
        s._running = True
        s._run_slot(s.bands[0])
        assert h.ticks >= 5

    def test_raising_dwell_fn_falls_back_to_default(self):
        h = _Harness()
        s = self._sched(h, lambda band: (_ for _ in ()).throw(RuntimeError("x")))
        s._running = True
        s._run_slot(s.bands[0])
        assert h.ticks == 10        # the configured default


class TestClassBalancedRotation:
    """A flat rotation gives each FREQUENCY equal airtime, not each CLASS.

    FM is carried by 17 presets and APRS by one, so a flat list collected FM 17x
    faster and drove the live corpus to a 20.9x imbalance (FM 1191, APRS 57) —
    which teaches a model to guess the majority class.
    """

    def _bands_uneven(self):
        return ([{"id": "fm%d" % i, "freq_hz": 160_000_000 + i, "label": "FM"}
                 for i in range(5)] +
                [{"id": "aprs", "freq_hz": 144_390_000, "label": "AFSK1200"}])

    def _sched(self, h):
        return IQCollectScheduler(
            self._bands_uneven(), h.start_slot, h.stop_slot, h.is_enabled,
            dwell_s=5, idle_s=0, sleep_fn=h.sleep)

    def test_each_class_gets_one_slot_per_rotation(self):
        h = _Harness()
        s = self._sched(h)
        s._running = True
        for _ in range(6):                      # three full rotations of 2 classes
            label = s._labels[s._index]
            band = s._groups[label][s._cursor[label] % len(s._groups[label])]
            s._cursor[label] += 1
            s._index = (s._index + 1) % len(s._labels)
            s._run_slot(band)
        labels = [b for b in h.started]
        fm = len([x for x in labels if x.startswith("fm")])
        aprs = len([x for x in labels if x == "aprs"])
        assert fm == aprs, "classes must get equal slots, got FM=%d APRS=%d" % (fm, aprs)

    def test_frequency_advances_within_a_class(self):
        """Diversity is still wanted — just spread across rotations."""
        h = _Harness()
        s = self._sched(h)
        seen = []
        for _ in range(3):
            band = s._groups["FM"][s._cursor["FM"] % 5]
            s._cursor["FM"] += 1
            seen.append(band["id"])
        assert len(set(seen)) == 3, "same frequency reused every rotation"

    def test_snapshot_reports_frequencies_per_class(self):
        h = _Harness()
        s = self._sched(h)
        snap = s.snapshot()
        assert snap["frequencies_per_class"] == {"FM": 5, "AFSK1200": 1}
        assert snap["classes"] == ["AFSK1200", "FM"]


class TestDutyGating:
    """A random window on a BURSTY channel is almost always empty.

    Collecting those directly produced 1921 "OOK" samples containing zero
    transmissions, and "AFSK1200" that was a steady carrier parked on 144.390
    rather than an APRS packet. For a bursty protocol the label is only true
    during a burst, so those bands must come from the segmenter.
    """

    def test_presets_declare_a_duty(self):
        from ravensdr.presets import get_presets
        missing = [p["id"] for p in get_presets() if "duty" not in p]
        assert not missing, "presets without duty: %s" % missing

    def test_duty_values_are_valid(self):
        from ravensdr.presets import get_presets
        bad = [p["id"] for p in get_presets()
               if p["duty"] not in ("continuous", "burst")]
        assert not bad

    def test_always_on_broadcasts_are_continuous(self):
        from ravensdr.presets import get_preset_by_id
        for pid in ("noaa-seattle", "kuow-fm", "kexp-fm"):
            assert get_preset_by_id(pid)["duty"] == "continuous"

    def test_packet_and_sensor_bands_are_bursty(self):
        """These transmit for milliseconds and are silent the rest of the time."""
        from ravensdr.presets import get_preset_by_id
        for pid in ("aprs-144390", "acars-vhf", "ism-security-345",
                    "pager-pocsag", "redmond-ares"):
            assert get_preset_by_id(pid)["duty"] == "burst", pid

    def test_band_dicts_carry_duty_to_the_scheduler(self):
        from ravensdr.presets import get_preset_by_id
        band = {"id": "x", "freq_hz": 1, "label": "FM",
                "duty": get_preset_by_id("noaa-seattle")["duty"]}
        assert band["duty"] == "continuous"


class TestNonIqModesNeverLabel:
    """Presets whose IQ never reaches the classifier must not label the corpus.

    ADS-B is map-only on the second dongle; AIS runs its own decoder. Neither
    feeds the main receive path, so tuning one used to leave a stale
    collect_label behind while the receiver sat on something else. That filed
    ten windows of 162.550 MHz NOAA weather under "ADSB" — a class the model
    does not have — which would have poisoned the next training run.

    The rotation and the label assignment share one exclusion set precisely so
    they cannot drift apart; these tests hold both ends of that.
    """

    def test_adsb_and_ais_are_excluded(self):
        from ravensdr.app import NON_IQ_MODES
        assert "adsb" in NON_IQ_MODES
        assert "ais" in NON_IQ_MODES

    def test_rotation_skips_non_iq_presets(self, monkeypatch):
        import ravensdr.app as app

        presets = [
            {"id": "noaa-seattle", "freq": "162.550M", "mode": "fm",
             "expected_modulation": "FM", "duty": "continuous"},
            {"id": "adsb-1090", "freq": "1090M", "mode": "adsb",
             "expected_modulation": "ADSB", "duty": "burst", "device_index": 1},
            {"id": "ais-marine", "freq": "161.975M", "mode": "ais",
             "expected_modulation": "FM", "duty": "burst"},
        ]
        monkeypatch.setattr(app, "get_presets", lambda: presets)

        ids = {b["id"] for b in app._collect_bands()}
        assert "noaa-seattle" in ids
        assert "adsb-1090" not in ids, "ADS-B is on a different dongle entirely"
        assert "ais-marine" not in ids, "AIS never reaches the classifier"

    def test_no_band_is_labelled_adsb(self, monkeypatch):
        """The label must describe the modulation, not the application."""
        import ravensdr.app as app
        monkeypatch.setattr(app, "get_presets", lambda: [
            {"id": "adsb-1090", "freq": "1090M", "mode": "adsb",
             "expected_modulation": "ADSB", "duty": "burst"},
        ])
        assert app._collect_bands() == []


class TestOperatorYield:
    """Corpus building must never outrank a person.

    One dongle, three claimants, and they are not equally urgent. A satellite
    pass happens now or not at all, so it may pre-empt. IQ collection is
    opportunistic — waiting costs nothing — so it has no business holding the
    radio while somebody is listening. It used to, which is how tuning to a
    weather preset produced a silently empty transcript.
    """

    def _sched(self, h, should_yield):
        return IQCollectScheduler(
            _bands(), h.start_slot, h.stop_slot, h.is_enabled,
            sleep_fn=h.sleep, dwell_s=30, idle_s=0,
            should_yield=should_yield)

    def test_dwell_is_cut_short_when_the_operator_tunes(self):
        h = _Harness()
        holding = {"v": False}
        sched = self._sched(h, lambda: holding["v"])
        sched._running = True

        # Ten seconds in, somebody tunes.
        real_sleep = h.sleep

        def sleep(sec):
            real_sleep(sec)
            if h.ticks == 10:
                holding["v"] = True

        h.sleep = sleep
        sched._sleep = sleep
        sched._run_slot(_bands()[0])

        # Yielded promptly rather than serving out a 30s dwell.
        assert h.ticks <= 12, "held the radio for %d ticks" % h.ticks
        assert h.stopped == ["fm"], "must release the band it was holding"

    def test_full_dwell_when_nobody_is_listening(self):
        h = _Harness()
        sched = self._sched(h, lambda: False)
        sched._running = True
        sched._sleep = h.sleep
        sched._run_slot(_bands()[0])
        assert h.ticks == 30, "an idle radio should serve the whole dwell"

    def test_yield_defaults_to_never_when_unwired(self):
        """A scheduler built without the predicate must behave as before."""
        h = _Harness()
        sched = IQCollectScheduler(
            _bands(), h.start_slot, h.stop_slot, h.is_enabled,
            sleep_fn=h.sleep, dwell_s=5, idle_s=0)
        sched._running = True
        sched._sleep = h.sleep
        sched._run_slot(_bands()[0])
        assert h.ticks == 5


class TestManualBurstTimeout:
    """A burst must be given time proportional to what was asked for.

    Collection is rate-limited to one sample per class every 2s, and gated
    windows are skipped on top of that — a measured burst averaged ~7.8s per
    sample. A flat 180s budget silently truncated anything over ~25 samples,
    which is how a 30-sample request finished with 7 unfilled.
    """

    def _t(self, n):
        from ravensdr.app import _manual_burst_timeout
        return _manual_burst_timeout(n)

    def test_small_requests_get_a_floor(self):
        assert self._t(1) == 60
        assert self._t(5) == 60

    def test_budget_scales_with_count(self):
        # The case that failed: 30 samples needed well over the old flat 180s.
        assert self._t(30) >= 240
        assert self._t(100) > self._t(30)

    def test_large_requests_are_capped(self):
        """A big count must not let a burst hold the dongle indefinitely."""
        assert self._t(10_000) == 900


class TestUnknownClassCollection:
    """Negative examples are the gap that blocks a spectrum sweep.

    With no "none of the above" class the model must force every input into one
    of the six it knows, so it can never decline — point it at an unfamiliar
    transmitter and it will confidently call it FM. But "unknown" is also what a
    preset that never declared a modulation looks like, and collecting THAT
    would file arbitrary signals under a class name. Intent is the difference.
    """

    def _clf(self, tmp_path, monkeypatch):
        import ravensdr.signal_classifier as sc
        monkeypatch.setattr(sc, "COLLECTED_DIR", str(tmp_path))
        return sc.SignalClassifier(onnx_path=None, hef_path=None)

    def test_accidental_unknown_is_still_refused(self, tmp_path, monkeypatch):
        import numpy as np
        clf = self._clf(tmp_path, monkeypatch)
        iq = (np.random.randn(24000) + 1j * np.random.randn(24000)).astype(np.complex64)
        assert clf.collect_sample(iq, "unknown", 433_920_000) is None

    def test_deliberate_unknown_burst_collects(self, tmp_path, monkeypatch):
        import numpy as np
        clf = self._clf(tmp_path, monkeypatch)
        clf.collect_burst(3, "unknown")
        iq = (np.random.randn(24000) + 1j * np.random.randn(24000)).astype(np.complex64)
        assert clf.collect_sample(iq, "unknown", 433_920_000) is not None

    def test_a_burst_for_another_class_does_not_unlock_unknown(
            self, tmp_path, monkeypatch):
        import numpy as np
        clf = self._clf(tmp_path, monkeypatch)
        clf.collect_burst(3, "OOK")
        iq = (np.random.randn(24000) + 1j * np.random.randn(24000)).astype(np.complex64)
        assert clf.collect_sample(iq, "unknown", 433_920_000) is None
