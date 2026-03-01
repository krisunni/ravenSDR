# Integration test — NOAA weather radio web stream capture + parse

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from ravensdr.noaa_parser import parse_weather_transcript


@pytest.mark.integration
@pytest.mark.network
class TestNoaaWebStream:
    """Integration test that captures NOAA weather radio from a web stream,
    transcribes with Whisper (CPU fallback), and parses the transcript.

    Requires internet access and faster-whisper installed.
    Skips gracefully if stream is unavailable or dependencies missing.
    """

    def test_capture_and_parse_noaa_stream(self):
        """Capture 30s of NOAA audio, transcribe, and parse weather fields."""
        # Check dependencies
        try:
            import subprocess
            import numpy as np
        except ImportError:
            pytest.skip("numpy not available")

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            pytest.skip("faster-whisper not available")

        stream_url = "https://wxradio.org/CA-Monterey-KEC49"

        # Capture 30 seconds of audio via ffmpeg
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", stream_url,
                    "-t", "30",
                    "-ar", "16000",
                    "-ac", "1",
                    "-f", "s16le",
                    "-acodec", "pcm_s16le",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=60,
            )
        except FileNotFoundError:
            pytest.skip("ffmpeg not installed")
        except subprocess.TimeoutExpired:
            pytest.skip("Stream capture timed out")

        pcm_data = result.stdout
        if len(pcm_data) < 16000 * 2:  # less than 1 second
            pytest.skip("Stream unavailable or too short")

        # Transcribe with faster-whisper CPU
        samples = np.frombuffer(pcm_data, dtype=np.int16).astype(np.float32) / 32768.0
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(samples, language="en", beam_size=1)
        text = " ".join(seg.text for seg in segments)

        assert len(text) > 0, "Whisper produced empty transcript"

        # Parse
        parsed = parse_weather_transcript(text)

        # At least some fields should be extracted from a real NOAA broadcast
        has_temp = parsed["temperature"] is not None
        has_wind = parsed["wind"] is not None
        has_vis = parsed["visibility"] is not None

        assert has_temp or has_wind or has_vis, (
            f"No weather fields parsed from transcript: {text[:200]}"
        )
