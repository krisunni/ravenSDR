# WEFAX receiver — rtl_fm HF direct sampling + numpy WEFAX decode

import datetime
import glob
import logging
import os
import select
import subprocess
import threading
import time
import wave

import numpy as np

log = logging.getLogger(__name__)

RAW_DIR = "/tmp/ravensdr/wefax"
IMAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "images", "wefax")

# WEFAX standard
IOC = 576  # lines per minute (IOC 576 for NMC/NOJ)
IMAGE_WIDTH = 1809  # pixels

# Frequency offset — WEFAX convention: tune 1.9 kHz below listed frequency
FREQ_OFFSET_KHZ = -1.9

# Recording parameters. rtl_fm is told -s 12k and outputs mono 16-bit PCM at
# 12000 Hz; we write that straight to a WAV (the decoder reads the rate back).
SAMPLE_RATE = "12k"
CAPTURE_RATE_HZ = 12000


class WefaxReceiver:
    """Records HF WEFAX broadcasts via rtl_fm direct sampling and decodes with numpy."""

    def __init__(self, emit_fn=None, device_index=0):
        self.emit_fn = emit_fn or (lambda *a, **kw: None)
        self.device_index = device_index
        self._recording = False
        self._process = None
        self._thread = None
        self._current_broadcast = None

    @property
    def is_recording(self):
        return self._recording

    @property
    def current_broadcast(self):
        return self._current_broadcast

    def record_broadcast(self, broadcast_info):
        """Start recording a WEFAX broadcast in a background thread."""
        if self._recording:
            log.warning("Already recording WEFAX — skipping %s", broadcast_info.get("description"))
            return False

        self._current_broadcast = broadcast_info
        self._thread = threading.Thread(
            target=self._record_and_decode,
            args=(broadcast_info,),
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
        self._current_broadcast = None

    def get_latest_image(self, chart_type=None):
        """Return metadata for the most recently decoded WEFAX chart."""
        image_dir = os.path.abspath(IMAGE_DIR)
        if not os.path.isdir(image_dir):
            return None

        pattern = os.path.join(image_dir, "*.png")
        images = sorted(glob.glob(pattern), reverse=True)

        for img_path in images:
            meta = self._parse_filename(os.path.basename(img_path))
            if chart_type and meta.get("chart_type") != chart_type:
                continue
            return meta

        return None

    def get_image_history(self, count=10, chart_type=None):
        """Return metadata for the last N decoded charts."""
        image_dir = os.path.abspath(IMAGE_DIR)
        if not os.path.isdir(image_dir):
            return []

        images = sorted(glob.glob(os.path.join(image_dir, "*.png")), reverse=True)
        history = []
        for img_path in images:
            meta = self._parse_filename(os.path.basename(img_path))
            if chart_type and meta.get("chart_type") != chart_type:
                continue
            history.append(meta)
            if len(history) >= count:
                break
        return history

    def _record_and_decode(self, broadcast_info):
        """Record rtl_fm audio, then decode WEFAX with the numpy decoder."""
        station = broadcast_info.get("station", "NMC")
        freq_khz = broadcast_info.get("frequency_khz", 8682.0)
        chart_type = broadcast_info.get("chart_type", "surface_analysis")
        duration_min = broadcast_info.get("duration_minutes", 10)
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H%MZ")

        os.makedirs(RAW_DIR, exist_ok=True)
        os.makedirs(os.path.abspath(IMAGE_DIR), exist_ok=True)

        basename = f"{station}_{freq_khz:.0f}kHz_{chart_type}_{timestamp}"
        wav_file = os.path.join(RAW_DIR, f"{basename}.wav")
        png_file = os.path.join(os.path.abspath(IMAGE_DIR), f"{basename}.png")

        # Apply frequency offset (tune 1.9 kHz below listed frequency)
        tuned_khz = freq_khz + FREQ_OFFSET_KHZ
        tuned_hz = int(tuned_khz * 1000)

        self._recording = True
        log.info("WEFAX recording: %s %s at %.1f kHz (tuned %.1f kHz) for %d min",
                 station, chart_type, freq_khz, tuned_khz, duration_min)

        duration_sec = duration_min * 60

        try:
            # rtl_fm tuned directly to the HF frequency (V4 has an internal upconverter).
            # Read its raw PCM straight into a WAV in Python — no sox middleman (the
            # old rtl_fm | sox pipe died silently and produced 44-byte empty files).
            rtl_cmd = self.build_rtl_fm_cmd(tuned_hz)
            rtl_proc = subprocess.Popen(
                rtl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._process = rtl_proc
            log.info("WEFAX rtl_fm started (pid %d): %s", rtl_proc.pid, " ".join(rtl_cmd))

            # Log rtl_fm stderr in background (PLL lock, tuner info)
            stderr_thread = threading.Thread(
                target=self._log_rtl_stderr, args=(rtl_proc.stderr,), daemon=True
            )
            stderr_thread.start()

            bytes_written = self._capture_to_wav(rtl_proc, wav_file, duration_sec)
            log.info("WEFAX capture finished: %.1f MB over ~%d s (rtl_fm exit=%s)",
                     bytes_written / 1e6, duration_sec, rtl_proc.poll())
            if bytes_written == 0:
                log.error("WEFAX rtl_fm produced NO audio — device busy, tuning failed, "
                          "or nothing on frequency. Check the rtl_fm log lines above.")

        except FileNotFoundError as e:
            log.error("WEFAX recording failed — command not found: %s", e)
            self._recording = False
            self._current_broadcast = None
            return
        except Exception as e:
            log.error("WEFAX recording error: %s", e)
            self._recording = False
            self._current_broadcast = None
            return
        finally:
            # Always kill rtl_fm process to prevent orphans holding the USB device
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait()
                log.info("WEFAX rtl_fm process cleaned up in finally block")
            self._process = None

        if not os.path.exists(wav_file):
            log.error("WEFAX WAV file not created: %s", wav_file)
            self._recording = False
            self._current_broadcast = None
            return

        file_size = os.path.getsize(wav_file)
        log.info("WEFAX recording complete: %s (%.1f MB)",
                 wav_file, file_size / 1e6)

        # Sanity check — if WAV is tiny, rtl_fm likely failed to start
        if file_size < 10000:
            log.error("WEFAX WAV too small (%.0f bytes) — rtl_fm likely failed. "
                      "Check rtl_fm stderr logs above.", file_size)
            self._recording = False
            self._current_broadcast = None
            try:
                os.remove(wav_file)
            except OSError:
                pass
            return

        # Analyze signal level from the recorded WAV
        self._analyze_wav_signal(wav_file, station, freq_khz)

        # Decode with the built-in numpy WEFAX decoder
        decode_ok = self._decode_wefax(wav_file, png_file)
        self._recording = False
        self._current_broadcast = None

        if not decode_ok:
            return

        # Clean up raw WAV
        try:
            os.remove(wav_file)
            log.info("Cleaned up raw WAV: %s", wav_file)
        except OSError:
            pass

        # Emit event
        filename = os.path.basename(png_file)
        self.emit_fn("wefax_image_ready", {
            "url": f"/static/images/wefax/{filename}",
            "station": station,
            "frequency_khz": freq_khz,
            "chart_type": chart_type,
            "decoded_at": datetime.datetime.utcnow().isoformat() + "Z",
            "image_width": IMAGE_WIDTH,
            "ioc": IOC,
        })
        log.info("WEFAX image decoded: %s", png_file)

    def _capture_to_wav(self, rtl_proc, wav_file, duration_sec):
        """Stream rtl_fm's raw PCM stdout into a mono 16-bit WAV for `duration_sec`.

        Returns total PCM bytes written. Uses select() so a stalled/dead rtl_fm
        can't block the loop, and aborts early if no audio arrives at all.
        """
        fd = rtl_proc.stdout.fileno()
        deadline = time.monotonic() + duration_sec
        bytes_written = 0
        started = time.monotonic()

        with wave.open(wav_file, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(CAPTURE_RATE_HZ)
            while self._recording and time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 1.0)
                if ready:
                    chunk = os.read(fd, 65536)
                    if not chunk:            # rtl_fm closed stdout / exited
                        break
                    wf.writeframes(chunk)
                    bytes_written += len(chunk)
                else:
                    # No data this second — bail if rtl_fm died or never produced audio
                    if rtl_proc.poll() is not None:
                        break
                    if bytes_written == 0 and time.monotonic() - started > 4:
                        log.error("WEFAX: no audio from rtl_fm after 4 s — aborting capture")
                        break
        return bytes_written

    def _decode_wefax(self, wav_file, png_file):
        """Decode WEFAX audio to PNG using the built-in numpy decoder.

        Replaces the old fldigi path (fldigi has no headless WAV->PNG mode).
        See ravensdr/wefax_decode.py for the FM-demodulation pipeline.
        """
        try:
            from ravensdr.wefax_decode import decode_wav_to_png
            meta = decode_wav_to_png(wav_file, png_file)
        except Exception as e:
            log.error("WEFAX decode error: %s", e, exc_info=True)
            return False

        if not meta or not os.path.exists(png_file):
            log.error("WEFAX decode produced no image from %s", wav_file)
            return False

        log.info("WEFAX decoded: %d lines, lpm=%.2f, mean=%.2f",
                 meta["lines"], meta["lpm"], meta["mean_brightness"])
        return True

    @staticmethod
    def _log_rtl_stderr(stderr):
        """Log rtl_fm stderr for diagnostics (PLL lock, tuner info)."""
        try:
            for line in stderr:
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    log.info("rtl_fm(wefax): %s", msg)
        except (ValueError, OSError):
            pass

    @staticmethod
    def _analyze_wav_signal(wav_file, station, freq_khz):
        """Analyze the recorded WAV file and log signal quality metrics."""
        try:
            # Read raw PCM from the WAV file (skip 44-byte header)
            with open(wav_file, "rb") as f:
                header = f.read(44)
                # Read first 30 seconds for analysis (11025 Hz * 2 bytes * 30s)
                raw = f.read(11025 * 2 * 30)

            if len(raw) < 1024:
                log.warning("WEFAX signal: WAV too small for analysis (%d bytes)", len(raw))
                return

            samples = np.frombuffer(raw[:len(raw) - len(raw) % 2], dtype=np.int16).astype(np.float64)
            rms = np.sqrt(np.mean(samples ** 2))
            peak = int(np.max(np.abs(samples)))
            if rms > 0:
                rms_db = 20 * np.log10(rms)
            else:
                rms_db = -100.0

            # Estimate signal quality
            if rms > 1000:
                quality = "STRONG"
            elif rms > 500:
                quality = "GOOD"
            elif rms > 100:
                quality = "WEAK"
            else:
                quality = "NO SIGNAL (noise floor only)"

            log.info("WEFAX signal %s %.1f kHz: RMS=%.0f (%.1f dB) peak=%d — %s",
                     station, freq_khz, rms, rms_db, peak, quality)

            if rms < 100:
                log.warning("WEFAX signal too weak — check antenna and frequency. "
                            "Long wire (5-10m) strongly recommended for HF.")

        except Exception as e:
            log.warning("WEFAX signal analysis failed: %s", e)

    def build_rtl_fm_cmd(self, tuned_hz):
        """Build rtl_fm command for WEFAX HF reception on the RTL-SDR Blog V4.

        The V4 has a built-in HF upconverter, so HF is received by tuning the
        tuner DIRECTLY to the frequency. It does NOT use direct sampling — that
        was the V3 method; forcing `-E direct2` on a V4 mistunes the front end
        and yields no usable audio. USB demodulation via -M usb.
        """
        cmd = [
            "rtl_fm",
            "-f", str(tuned_hz),
            "-M", "usb",         # Upper sideband demodulation
            "-s", SAMPLE_RATE,   # rtl_fm outputs mono 16-bit PCM at this rate (12 kHz)
        ]
        if self.device_index > 0:
            cmd.extend(["-d", str(self.device_index)])
        cmd.append("-")
        return cmd

    @staticmethod
    def _parse_filename(filename):
        """Parse WEFAX filename into metadata dict."""
        # Format: NMC_8682kHz_surface_analysis_2026-03-16T1230Z.png
        name = filename.replace(".png", "")
        parts = name.split("_", 2)  # station, freq, rest

        station = parts[0] if len(parts) > 0 else "Unknown"
        freq_str = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        # Parse frequency
        freq_khz = 0.0
        if freq_str.endswith("kHz"):
            try:
                freq_khz = float(freq_str.replace("kHz", ""))
            except ValueError:
                pass

        # Split rest into chart_type and timestamp
        # chart_type may contain underscores, timestamp is the last part
        if rest:
            # Timestamp is always the last segment after the last underscore
            # Format: ...chart_type_2026-03-16T1230Z
            last_under = rest.rfind("_")
            if last_under > 0:
                chart_type = rest[:last_under]
                decoded_at = rest[last_under + 1:]
            else:
                chart_type = rest
                decoded_at = ""
        else:
            chart_type = ""
            decoded_at = ""

        return {
            "url": f"/static/images/wefax/{filename}",
            "station": station,
            "frequency_khz": freq_khz,
            "chart_type": chart_type,
            "decoded_at": decoded_at,
            "filename": filename,
        }
