import json

import pytest

from ravensdr import config


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    """Point the config module at a throwaway config.json."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", str(path))
    return path


class TestSettings:
    def test_defaults_present(self, cfg_file):
        s = config.get_settings()
        assert s["keywords"] == []
        assert s["keywords_enabled"] is True
        assert s["sei_match_threshold"] == 0.85
        assert s["classifier_confidence"] == 0.7
        assert s["segmenter_threshold_db"] == 10
        assert s["silence_threshold"] == 500

    def test_update_persists_and_merges(self, cfg_file):
        config.update_settings({"sei_match_threshold": 0.7})
        # partial update leaves other keys untouched
        s = config.get_settings()
        assert s["sei_match_threshold"] == 0.7
        assert s["classifier_confidence"] == 0.7  # default preserved

    def test_keywords_round_trip(self, cfg_file):
        kws = [{"term": "mayday", "severity": "critical", "enabled": True}]
        config.update_settings({"keywords": kws})
        assert config.get_settings()["keywords"] == kws

    def test_backfills_missing_settings_block(self, cfg_file):
        # Older config with no settings key
        cfg_file.write_text(json.dumps({"last_preset": "noaa-monterey"}))
        s = config.get_settings()
        assert s["sei_match_threshold"] == 0.85  # backfilled default

    def test_settings_dont_clobber_other_config(self, cfg_file):
        config.set_secondary_task("adsb")
        config.update_settings({"silence_threshold": 300})
        cfg = config.load_config()
        assert cfg["secondary_dongle"]["task"] == "adsb"
        assert cfg["settings"]["silence_threshold"] == 300


class TestApplySettings:
    """The per-module apply_settings hooks the Settings tab drives."""

    def test_sei_model_applies_threshold(self):
        from ravensdr.sei_model import SEIModel
        m = SEIModel()
        m.apply_settings({"sei_match_threshold": 0.6})
        assert m.match_threshold == 0.6
        # bad value is ignored, keeps prior
        m.apply_settings({"sei_match_threshold": "nan-ish"})
        assert m.match_threshold == 0.6

    def test_segmenter_applies_threshold(self):
        from ravensdr.iq_segmenter import IQSegmenter
        seg = IQSegmenter()
        seg.apply_settings({"segmenter_threshold_db": 15})
        assert seg.threshold_db == 15

    def test_classifier_applies_confidence(self):
        from ravensdr.signal_classifier import SignalClassifier
        c = SignalClassifier()
        c.apply_settings({"classifier_confidence": 0.4})
        assert c.confidence_threshold == 0.4


class TestStartupPreset:
    def test_defaults_to_none_when_nothing_tuned(self, cfg_file):
        assert config.get_startup_preset(config.load_config()) is None

    def test_resumes_last_tuned_preset(self, cfg_file):
        config.set_last_preset("noaa-monterey")
        assert config.get_startup_preset(config.load_config()) == "noaa-monterey"

    def test_pinned_default_wins_over_last_tuned(self, cfg_file):
        config.set_last_preset("noaa-monterey")
        cfg = config.load_config()
        cfg["startup"]["default_preset"] = "noaa-seattle"
        config.save_config(cfg)
        assert config.get_startup_preset(config.load_config()) == "noaa-seattle"

    def test_auto_tune_disabled_starts_idle(self, cfg_file):
        config.set_last_preset("noaa-monterey")
        cfg = config.load_config()
        cfg["startup"]["auto_tune"] = False
        config.save_config(cfg)
        assert config.get_startup_preset(config.load_config()) is None

    def test_missing_startup_section_defaults_to_auto_tune(self, cfg_file):
        # Config written by an older version has no "startup" key
        cfg_file.write_text(json.dumps({"last_preset": "noaa-monterey"}))
        assert config.get_startup_preset(config.load_config()) == "noaa-monterey"


class TestSetLastPreset:
    def test_persists_across_loads(self, cfg_file):
        config.set_last_preset("kuow-fm")
        assert config.load_config()["last_preset"] == "kuow-fm"

    def test_overwrites_previous(self, cfg_file):
        config.set_last_preset("kuow-fm")
        config.set_last_preset("noaa-portland")
        assert config.load_config()["last_preset"] == "noaa-portland"

    def test_repeat_write_is_a_no_op(self, cfg_file):
        config.set_last_preset("kuow-fm")
        mtime = cfg_file.stat().st_mtime_ns
        config.set_last_preset("kuow-fm")
        assert cfg_file.stat().st_mtime_ns == mtime

    def test_preserves_unrelated_config(self, cfg_file):
        config.set_secondary_task("adsb")
        config.set_last_preset("kuow-fm")
        cfg = config.load_config()
        assert cfg["secondary_dongle"]["task"] == "adsb"
        assert cfg["last_preset"] == "kuow-fm"
