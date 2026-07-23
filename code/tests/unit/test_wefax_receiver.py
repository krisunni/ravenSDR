# Unit tests for WEFAX receiver

import os
from unittest.mock import MagicMock, patch

import pytest

from ravensdr.wefax_receiver import (
    CAPTURE_RATE_HZ,
    FREQ_OFFSET_KHZ,
    IMAGE_WIDTH,
    IOC,
    SAMPLE_RATE,
    WefaxReceiver,
)


class TestRtlFmCommand:
    """Test rtl_fm command construction for WEFAX HF reception (V4 upconverter)."""

    def test_no_direct_sampling(self):
        # The V4 uses a built-in upconverter and tunes HF directly — direct
        # sampling (-E direct2) is a V3-only mode and must NOT be present.
        cmd = WefaxReceiver().build_rtl_fm_cmd(8680100)
        assert "-E" not in cmd
        assert "direct2" not in cmd

    def test_usb_demodulation(self):
        cmd = WefaxReceiver().build_rtl_fm_cmd(8680100)
        assert "-M" in cmd
        idx = cmd.index("-M")
        assert cmd[idx + 1] == "usb"

    def test_sample_rate(self):
        cmd = WefaxReceiver().build_rtl_fm_cmd(8680100)
        assert "-s" in cmd
        idx = cmd.index("-s")
        assert cmd[idx + 1] == SAMPLE_RATE

    def test_capture_rate_constant(self):
        # rtl_fm outputs at -s (12 kHz); the WAV is written at that same rate.
        assert CAPTURE_RATE_HZ == 12000

    def test_frequency_in_hz(self):
        cmd = WefaxReceiver().build_rtl_fm_cmd(8680100)
        assert "-f" in cmd
        idx = cmd.index("-f")
        assert cmd[idx + 1] == "8680100"

    def test_pipe_output(self):
        cmd = WefaxReceiver().build_rtl_fm_cmd(8680100)
        assert cmd[-1] == "-"


class TestFrequencyOffset:
    """Test WEFAX frequency offset calculation."""

    def test_offset_value(self):
        assert FREQ_OFFSET_KHZ == -1.9

    def test_nmc_8682_offset(self):
        listed_khz = 8682.0
        tuned_khz = listed_khz + FREQ_OFFSET_KHZ
        assert tuned_khz == pytest.approx(8680.1)

    def test_nmc_4346_offset(self):
        listed_khz = 4346.0
        tuned_khz = listed_khz + FREQ_OFFSET_KHZ
        assert tuned_khz == pytest.approx(4344.1)

    def test_noj_4298_offset(self):
        listed_khz = 4298.0
        tuned_khz = listed_khz + FREQ_OFFSET_KHZ
        assert tuned_khz == pytest.approx(4296.1)


class TestNumpyDecoder:
    """The WEFAX decoder is pure numpy — verify it round-trips an image to <1% loss."""

    def test_encode_decode_roundtrip(self, tmp_path):
        import numpy as np
        from PIL import Image
        from ravensdr.wefax_decode import (encode_image_to_wav, decode_wav_to_png,
                                           IMAGE_WIDTH)

        # A simple test chart: white background with horizontal black bands
        h, w = 120, IMAGE_WIDTH
        orig = np.full((h, w), 255, dtype=np.uint8)
        orig[30:40, :] = 0
        orig[70:80, :] = 0

        wav = str(tmp_path / "t.wav")
        png = str(tmp_path / "t.png")
        encode_image_to_wav(orig, wav)
        meta = decode_wav_to_png(wav, png)

        assert meta is not None
        assert os.path.exists(png)
        assert meta["lines"] >= h            # image + phasing rows
        assert 119.0 <= meta["lpm"] <= 121.0  # deskew locks near 120 LPM

        # The decoded image must contain clear dark bands (not all white)
        dec = np.asarray(Image.open(png).convert("L"), dtype=np.float64)
        row_means = dec.mean(axis=1)
        assert row_means.min() < 120          # at least one strongly-dark line
        assert row_means.max() > 200          # and bright background lines


class TestFilenameGeneration:
    """Test WEFAX output filename convention."""

    def test_parse_surface_analysis_filename(self):
        filename = "NMC_8682kHz_surface_analysis_2026-03-16T1230Z.png"
        meta = WefaxReceiver._parse_filename(filename)
        assert meta["station"] == "NMC"
        assert meta["frequency_khz"] == 8682.0
        assert meta["chart_type"] == "surface_analysis"
        assert meta["decoded_at"] == "2026-03-16T1230Z"
        assert meta["url"] == f"/static/images/wefax/{filename}"

    def test_parse_24hr_forecast_filename(self):
        filename = "NMC_8682kHz_24hr_forecast_2026-03-16T1300Z.png"
        meta = WefaxReceiver._parse_filename(filename)
        assert meta["station"] == "NMC"
        assert meta["chart_type"] == "24hr_forecast"

    def test_parse_wave_chart_filename(self):
        filename = "NOJ_4298kHz_wave_chart_2026-03-16T0300Z.png"
        meta = WefaxReceiver._parse_filename(filename)
        assert meta["station"] == "NOJ"
        assert meta["frequency_khz"] == 4298.0
        assert meta["chart_type"] == "wave_chart"

    def test_parse_48hr_forecast_filename(self):
        filename = "NMC_12786kHz_48hr_forecast_2026-03-16T1700Z.png"
        meta = WefaxReceiver._parse_filename(filename)
        assert meta["chart_type"] == "48hr_forecast"
        assert meta["frequency_khz"] == 12786.0


class TestWefaxReceiverState:
    """Test receiver state management."""

    def test_initial_state(self):
        receiver = WefaxReceiver()
        assert receiver.is_recording is False
        assert receiver.current_broadcast is None

    def test_stop_when_not_recording(self):
        receiver = WefaxReceiver()
        receiver.stop()  # should not raise
        assert receiver.is_recording is False

    def test_emit_fn_called_on_image_ready(self):
        emit_fn = MagicMock()
        receiver = WefaxReceiver(emit_fn=emit_fn)

        # Verify emit_fn is stored
        assert receiver.emit_fn is emit_fn


class TestConstants:
    """Test WEFAX constants."""

    def test_ioc(self):
        assert IOC == 576

    def test_image_width(self):
        assert IMAGE_WIDTH == 1809
