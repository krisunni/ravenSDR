# APT decoder — rtl_fm recording (direct to WAV, no sox) + CLI image decode
# (aptdec or noaa-apt).

import datetime
import glob
import logging
import os
import select
import shutil
import subprocess
import threading
import time
import wave

log = logging.getLogger(__name__)

RAW_DIR = "/tmp/ravensdr/apt"
IMAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "images", "apt")

# Recording parameters. rtl_fm FM-demodulates the 137 MHz downlink to a mono
# 16-bit WAV at 11025 Hz (containing APT's 2400 Hz AM subcarrier); the decoder
# turns that into an image.
RECORD_DURATION = 900  # 15 minutes
DEFAULT_GAIN = 40
SAMPLE_RATE = "60k"
CAPTURE_RATE_HZ = 11025

# APT image decoders in preference order (both take a WAV and write a PNG).
APT_DECODERS = ("aptdec", "noaa-apt")


def find_apt_decoder():
    """Return the first installed APT decoder binary name, or None."""
    for name in APT_DECODERS:
        if shutil.which(name):
            return name
    return None


def _sat_number(satellite):
    """'NOAA 15' / 'NOAA-19' -> 15 / 19 (aptdec needs the numeric id)."""
    for tok in satellite.replace("-", " ").split():
        if tok.isdigit():
            return int(tok)
    return None


class AptDecoder:
    """Records APT satellite passes via rtl_fm and decodes with noaa-apt."""

    def __init__(self, emit_fn=None):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self._recording = False
        self._process = None
        self._thread = None
        self._current_pass = None

    @property
    def is_recording(self):
        return self._recording

    @property
    def current_pass(self):
        return self._current_pass

    def record_pass(self, pass_info, gain=DEFAULT_GAIN):
        """Start recording an APT pass in a background thread."""
        if self._recording:
            log.warning("Already recording a pass — skipping %s", pass_info.get("satellite"))
            return False

        self._current_pass = pass_info
        self._thread = threading.Thread(
            target=self._record_and_decode,
            args=(pass_info, gain),
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self):
        """Stop any active recording."""
        self._recording = False
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = None
        self._current_pass = None

    def get_latest_image(self):
        """Return metadata for the most recently decoded APT image."""
        image_dir = os.path.abspath(IMAGE_DIR)
        if not os.path.isdir(image_dir):
            return None

        images = sorted(glob.glob(os.path.join(image_dir, "*.png")), reverse=True)
        if not images:
            return None

        filename = os.path.basename(images[0])
        # Parse filename: NOAA-19_2026-02-28T1430Z.png
        parts = filename.replace(".png", "").split("_", 1)
        satellite = parts[0].replace("-", " ") if parts else "Unknown"
        pass_time = parts[1] if len(parts) > 1 else ""

        return {
            "url": f"/static/images/apt/{filename}",
            "satellite": satellite,
            "pass_time": pass_time,
            "filename": filename,
        }

    def get_image_history(self, count=5):
        """Return metadata for the last N decoded images."""
        image_dir = os.path.abspath(IMAGE_DIR)
        if not os.path.isdir(image_dir):
            return []

        images = sorted(glob.glob(os.path.join(image_dir, "*.png")), reverse=True)
        history = []
        for img_path in images[:count]:
            filename = os.path.basename(img_path)
            parts = filename.replace(".png", "").split("_", 1)
            satellite = parts[0].replace("-", " ") if parts else "Unknown"
            pass_time = parts[1] if len(parts) > 1 else ""
            history.append({
                "url": f"/static/images/apt/{filename}",
                "satellite": satellite,
                "pass_time": pass_time,
                "filename": filename,
            })
        return history

    def _record_and_decode(self, pass_info, gain):
        """Record rtl_fm audio, then decode with noaa-apt."""
        satellite = pass_info.get("satellite", "NOAA-19")
        frequency = pass_info.get("frequency", "137.9125M")
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H%MZ")
        safe_name = satellite.replace(" ", "-")

        os.makedirs(RAW_DIR, exist_ok=True)
        os.makedirs(os.path.abspath(IMAGE_DIR), exist_ok=True)

        wav_file = os.path.join(RAW_DIR, f"{safe_name}_{timestamp}.wav")
        png_file = os.path.join(
            os.path.abspath(IMAGE_DIR), f"{safe_name}_{timestamp}.png"
        )

        # Record with rtl_fm piped through sox to create WAV
        self._recording = True
        log.info("APT recording started: %s at %s for %ds",
                 satellite, frequency, RECORD_DURATION)

        try:
            # rtl_fm FM-demodulates the pass; read its PCM straight into a WAV in
            # Python (no sox — that pipe died silently and produced empty files).
            rtl_cmd = self.build_rtl_fm_cmd(frequency, gain)
            rtl_proc = subprocess.Popen(
                rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            self._process = rtl_proc
            bytes_written = self._capture_to_wav(rtl_proc, wav_file, RECORD_DURATION)
        except FileNotFoundError as e:
            log.error("APT recording failed — command not found: %s", e)
            self._recording = False
            self._current_pass = None
            return
        except Exception as e:
            log.error("APT recording error: %s", e)
            self._recording = False
            self._current_pass = None
            return
        finally:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None

        if not os.path.exists(wav_file) or bytes_written == 0:
            log.error("APT WAV not created / empty (device busy or no signal): %s", wav_file)
            self._recording = False
            self._current_pass = None
            return

        log.info("APT recording complete: %s (%.1f MB)",
                 wav_file, os.path.getsize(wav_file) / 1e6)

        # Decode to a PNG with whichever decoder is installed
        decoder = find_apt_decoder()
        if not decoder:
            log.error("No APT decoder found (looked for: %s). Build one via setup.sh "
                      "(aptdec: github.com/Xerbo/aptdec).", ", ".join(APT_DECODERS))
            self._recording = False
            self._current_pass = None
            return

        png_file = self._decode(decoder, wav_file, png_file, satellite)
        self._recording = False
        self._current_pass = None
        if not png_file:
            return
        log.info("APT image decoded (%s): %s", decoder, png_file)

        # Clean up raw WAV
        try:
            os.remove(wav_file)
            log.info("Cleaned up raw WAV: %s", wav_file)
        except OSError:
            pass

        # Emit event (use the decoder's actual output filename)
        self.emit_fn("apt_image_ready", {
            "url": f"/static/images/apt/{os.path.basename(png_file)}",
            "satellite": satellite,
            "pass_time": timestamp,
            "max_elevation": pass_info.get("max_elevation", 0),
            "location": f"{pass_info.get('lat', OBSERVER_LAT)}N, {pass_info.get('lon', OBSERVER_LON)}W",
        })

    def _capture_to_wav(self, rtl_proc, wav_file, duration_sec):
        """Stream rtl_fm's raw PCM stdout into a mono 16-bit WAV for `duration_sec`.

        Returns total PCM bytes written; aborts early if no audio arrives.
        """
        fd = rtl_proc.stdout.fileno()
        deadline = time.monotonic() + duration_sec
        started = time.monotonic()
        bytes_written = 0
        with wave.open(wav_file, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(CAPTURE_RATE_HZ)
            while self._recording and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    wf.writeframes(chunk)
                    bytes_written += len(chunk)
                elif rtl_proc.poll() is not None:
                    break
                elif bytes_written == 0 and time.monotonic() - started > 5:
                    log.error("APT: no audio from rtl_fm after 5 s — aborting")
                    break
        return bytes_written

    def _decode(self, decoder, wav_file, png_file, satellite):
        """Run the APT decoder; return the produced PNG path, or None on failure."""
        out_dir = os.path.dirname(png_file)
        before = set(glob.glob(os.path.join(out_dir, "*.png")))
        cmd = self.build_decode_cmd(decoder, wav_file, png_file, satellite)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            log.error("APT decode timed out (%s)", decoder)
            return None
        except Exception as e:
            log.error("APT decode error (%s): %s", decoder, e)
            return None
        if result.returncode != 0:
            log.error("APT decode failed (%s): %s", decoder, result.stderr.strip()[:300])
            return None
        # aptdec appends an image-type suffix (e.g. -r) — find the new PNG.
        if os.path.exists(png_file):
            return png_file
        new = sorted(set(glob.glob(os.path.join(out_dir, "*.png"))) - before)
        if new:
            os.replace(new[-1], png_file)  # normalize to our expected name
            return png_file
        log.error("APT decode produced no PNG (%s)", decoder)
        return None

    @staticmethod
    def build_decode_cmd(decoder, wav_file, png_file, satellite=""):
        """Build the decode command for the detected decoder."""
        if decoder == "noaa-apt":
            return ["noaa-apt", wav_file, "-o", png_file, "--rotate", "auto"]
        # aptdec: -o filename, -d output dir, -s satellite id
        cmd = ["aptdec", "-i", "r", "-o", os.path.basename(png_file),
               "-d", os.path.dirname(png_file) or "."]
        sat_id = _sat_number(satellite)
        if sat_id and 15 <= sat_id <= 19:
            cmd += ["-s", str(sat_id)]
        cmd.append(wav_file)
        return cmd

    @staticmethod
    def build_rtl_fm_cmd(frequency, gain=DEFAULT_GAIN):
        """Build the rtl_fm command for APT recording."""
        return [
            "rtl_fm",
            "-f", frequency,
            "-M", "fm",
            "-s", SAMPLE_RATE,
            "-r", str(CAPTURE_RATE_HZ),
            "-g", str(gain),
            "-",
        ]


# Import observer coords for location in events
OBSERVER_LAT = "47.6740"
OBSERVER_LON = "122.1215"
