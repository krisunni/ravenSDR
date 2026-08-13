# RTL-FM process manager with pyrtlsdr IQ capture backend
#
# Dual-path: uses pyrtlsdr for direct IQ capture (provides raw IQ for
# signal classification + demodulated audio for transcription) when
# available, falls back to rtl_fm subprocess otherwise.

import logging
import os
import signal as _signal
import time

# Use REAL stdlib modules, not eventlet's green versions.
# Eventlet's patched subprocess/threading cause fd conflicts and broken wait().
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
    threading = original("threading")
except ImportError:
    import subprocess
    import threading

log = logging.getLogger(__name__)


def _check_pyrtlsdr():
    """Check if pyrtlsdr is available and compatible with installed librtlsdr."""
    try:
        from rtlsdr import RtlSdr  # noqa: F401
        return True
    except (ImportError, AttributeError, OSError) as e:
        # ImportError: pyrtlsdr not installed
        # AttributeError: librtlsdr missing symbols (e.g., Blog fork lacks rtlsdr_set_dithering)
        # OSError: librtlsdr.so not found
        log.info("pyrtlsdr not usable: %s", e)
        return False


def _kill_pid(pid):
    """Kill a process by PID using raw os calls (bypasses eventlet)."""
    try:
        os.kill(pid, _signal.SIGTERM)
    except OSError:
        return
    # Give it 2 seconds to exit
    for _ in range(20):
        try:
            result = os.waitpid(pid, os.WNOHANG)
            if result[0] != 0:
                return  # exited
        except ChildProcessError:
            return  # already reaped
        time.sleep(0.1)
    # Still alive — SIGKILL
    try:
        os.kill(pid, _signal.SIGKILL)
        os.waitpid(pid, 0)
    except OSError:
        pass


class Tuner:
    """Manages SDR reception — pyrtlsdr direct IQ or rtl_fm subprocess.

    When pyrtlsdr is available, uses direct IQ capture which provides
    raw IQ samples for signal classification while also demodulating
    audio for the transcription pipeline. Falls back to rtl_fm subprocess
    if pyrtlsdr is not installed.
    """

    # Bandwidth per demodulation mode (rtl_fm fallback path)
    MODE_SAMPLE_RATES = {
        # AM is NOT 200k. A US medium-wave channel is 10 kHz wide, so
        # demodulating 200 kHz of spectrum pulls in twenty channels' worth of
        # noise and buries the station in it. 24k gives +/-12 kHz, which covers
        # the channel and nothing else.
        #
        # It must also stay ABOVE the -r 16k output rate. rtl_fm's resampler
        # only decimates: asked to go from 12k up to 16k it emits digital
        # silence — measured, exactly 0.0 RMS on a station that reads 12,835 at
        # "-s 12k" with no -r. So 12k looks correct on paper and produces a
        # dead radio in this pipeline. Anything <= 16k here is a silent failure.
        "am": "24k",
        "fm": "200k",
        "wbfm": "200k",
    }

    def __init__(self, pcm_queue, audio_queue):
        self.pcm_queue = pcm_queue
        self.audio_queue = audio_queue

        # Check pyrtlsdr availability
        self._use_pyrtlsdr = _check_pyrtlsdr()

        if self._use_pyrtlsdr:
            from ravensdr.iq_capture import IQCapture
            self._iq = IQCapture(pcm_queue=pcm_queue, audio_queue=audio_queue)
            log.info("Tuner using pyrtlsdr (direct IQ capture)")
        else:
            self._iq = None
            log.info("Tuner using rtl_fm subprocess (pyrtlsdr not available)")

        # rtl_fm fallback state
        self._current_freq = None
        self._current_mode = "fm"
        self._squelch = 0
        self._gain = "auto"
        self._sample_rate = None
        self._deemp = None
        self._ppm = 0
        self._direct_sampling = 0
        self._is_running = False
        self._process = None
        self._pid = None
        self._thread = None
        self._stop_event = threading.Event()
        # Every rtl_fm pid we have spawned and not yet confirmed dead.
        #
        # self._pid alone is not enough to guarantee cleanup: _kill_pid() yields
        # (time.sleep is green-patched), so two overlapping tune() calls from
        # different greenthreads can interleave — A blocks in _kill_pid while B
        # runs a full stop + Popen, then A resumes and clears self._pid,
        # clobbering B's brand-new pid. That rtl_fm is then never killed and
        # squats on the dongle, so every later tune/APT/pager decoder fails with
        # usb_claim_interface -6 ("device busy"). Killing the whole set on stop
        # makes the leak unreachable regardless of interleaving.
        self._spawned_pids = set()

    # ── Properties (delegate to IQCapture or local state) ──

    @property
    def current_freq(self):
        if self._use_pyrtlsdr:
            return self._iq.current_freq
        return self._current_freq

    @current_freq.setter
    def current_freq(self, value):
        if self._use_pyrtlsdr:
            self._iq.current_freq = value
        self._current_freq = value

    @property
    def current_mode(self):
        if self._use_pyrtlsdr:
            return self._iq.current_mode
        return self._current_mode

    @current_mode.setter
    def current_mode(self, value):
        if self._use_pyrtlsdr:
            self._iq.current_mode = value
        self._current_mode = value

    @property
    def squelch(self):
        if self._use_pyrtlsdr:
            return self._iq.squelch
        return self._squelch

    @squelch.setter
    def squelch(self, value):
        if self._use_pyrtlsdr:
            self._iq.squelch = value
        self._squelch = value

    @property
    def gain(self):
        if self._use_pyrtlsdr:
            return self._iq.gain
        return self._gain

    @gain.setter
    def gain(self, value):
        if self._use_pyrtlsdr:
            self._iq.gain = value
        self._gain = value

    @property
    def sample_rate(self):
        if self._use_pyrtlsdr:
            return self._iq.sample_rate
        return self._sample_rate

    @sample_rate.setter
    def sample_rate(self, value):
        if self._use_pyrtlsdr:
            self._iq.sample_rate = value
        self._sample_rate = value

    @property
    def deemp(self):
        if self._use_pyrtlsdr:
            return self._iq.deemp
        return self._deemp

    @deemp.setter
    def deemp(self, value):
        if self._use_pyrtlsdr:
            self._iq.deemp = value
        self._deemp = value

    @property
    def ppm(self):
        if self._use_pyrtlsdr:
            return self._iq.ppm
        return self._ppm

    @ppm.setter
    def ppm(self, value):
        if self._use_pyrtlsdr:
            self._iq.ppm = value
        self._ppm = value

    @property
    def direct_sampling(self):
        if self._use_pyrtlsdr:
            return self._iq.direct_sampling
        return self._direct_sampling

    @direct_sampling.setter
    def direct_sampling(self, value):
        if self._use_pyrtlsdr:
            self._iq.direct_sampling = value
        self._direct_sampling = value

    @property
    def is_running(self):
        if self._use_pyrtlsdr:
            return self._iq.is_running
        return self._is_running

    @is_running.setter
    def is_running(self, value):
        self._is_running = value

    @property
    def effective_sample_rate(self):
        if self._use_pyrtlsdr:
            sr = self._iq.effective_sample_rate
            return f"{sr // 1000}k" if sr >= 1000 else str(sr)
        if self._sample_rate:
            return self._sample_rate
        return self.MODE_SAMPLE_RATES.get(self._current_mode or "fm", "200k")

    @property
    def effective_deemp(self):
        if self._use_pyrtlsdr:
            return self._iq.effective_deemp
        if self._deemp is not None:
            return self._deemp
        return self._current_mode in ("fm", "wbfm")

    # ── IQ callback (pyrtlsdr path only) ──

    def set_iq_callback(self, callback):
        """Set callback for raw IQ chunks (pyrtlsdr path only)."""
        if self._use_pyrtlsdr and self._iq:
            self._iq.set_iq_callback(callback)

    # ── Control methods ──

    def tune(self, freq, mode="fm"):
        if self._use_pyrtlsdr:
            return self._iq.tune(freq, mode)
        return self._tune_rtlfm(freq, mode)

    def stop(self):
        if self._use_pyrtlsdr:
            return self._iq.stop()
        return self._stop_rtlfm()

    def poll(self):
        if self._use_pyrtlsdr:
            return self._iq.poll()
        return self._poll_rtlfm()

    def set_squelch(self, level):
        if self._use_pyrtlsdr:
            self._iq.set_squelch(level)
        else:
            self._squelch = max(0, min(100, level))
            if self._is_running:
                self._tune_rtlfm(self._current_freq, self._current_mode)

    def set_gain(self, value):
        if self._use_pyrtlsdr:
            self._iq.set_gain(value)
        else:
            self._gain = value
            if self._is_running:
                self._tune_rtlfm(self._current_freq, self._current_mode)

    def set_sample_rate(self, value):
        if self._use_pyrtlsdr:
            self._iq.set_sample_rate(value)
        else:
            self._sample_rate = value
            if self._is_running:
                self._tune_rtlfm(self._current_freq, self._current_mode)

    def set_deemp(self, value):
        if self._use_pyrtlsdr:
            self._iq.set_deemp(value)
        else:
            self._deemp = value
            if self._is_running:
                self._tune_rtlfm(self._current_freq, self._current_mode)

    def set_ppm(self, value):
        if self._use_pyrtlsdr:
            self._iq.set_ppm(value)
        else:
            self._ppm = int(value)
            if self._is_running:
                self._tune_rtlfm(self._current_freq, self._current_mode)

    def set_direct_sampling(self, value):
        if self._use_pyrtlsdr:
            self._iq.set_direct_sampling(value)
        else:
            self._direct_sampling = int(value)
            if self._is_running:
                self._tune_rtlfm(self._current_freq, self._current_mode)

    # ── rtl_fm fallback implementation ──

    def _tune_rtlfm(self, freq, mode="fm"):
        self._stop_rtlfm()
        self._current_freq = freq
        self._current_mode = mode
        self._stop_event.clear()

        sr = self.effective_sample_rate
        gain_arg = [] if self._gain == "auto" else ["-g", str(self._gain)]
        deemp_arg = ["-E", "deemp"] if self.effective_deemp else []
        ppm_arg = ["-p", str(self._ppm)] if self._ppm != 0 else []
        # rtl_fm has NO -D flag. Direct sampling is an -E enable option, and
        # passing -D makes rtl_fm exit with "invalid option -- 'D'" and a usage
        # dump before it ever opens the device — so setting direct sampling
        # from the UI would have taken the radio off the air entirely. Latent
        # only because no preset sets it, but /api/... exposes the control.
        #
        # Rarely needed on this node in any case: the RTL-SDR Blog V4 has a
        # built-in HF upconverter (the driver reports "RTL-SDR Blog V4
        # Detected"), so tuning an HF or MW frequency directly just works.
        # Kept correct for V3 dongles and for forcing a branch deliberately.
        if self._direct_sampling == 1:
            ds_arg = ["-E", "direct"]
        elif self._direct_sampling == 2:
            ds_arg = ["-E", "direct2"]
        else:
            ds_arg = []
        cmd = [
            "rtl_fm",
            "-f", freq,
            "-M", mode,
            "-s", sr,
            "-r", "16k",
            "-l", str(self._squelch),
        ] + gain_arg + deemp_arg + ppm_arg + ds_arg + ["-"]

        log.info("Starting rtl_fm: %s", " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._pid = self._process.pid
            self._spawned_pids.add(self._pid)
        except FileNotFoundError:
            log.error("rtl_fm not found — is rtl-sdr installed?")
            raise

        self._is_running = True
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self._stderr_thread = threading.Thread(target=self._log_stderr, daemon=True)
        self._stderr_thread.start()

    def _stop_rtlfm(self):
        self._stop_event.set()
        pid = self._pid

        # ORDER MATTERS: kill the process BEFORE closing its pipes.
        #
        # _read_loop runs on a real OS thread blocked in stdout.read(4096), and
        # CPython's BufferedReader holds an internal lock for the duration of
        # that call. Closing the pipe first has to wait for that lock, so if no
        # bytes are coming the close never returns — and because _stop_rtlfm is
        # driven from the eventlet hub's thread, the ENTIRE app freezes (every
        # HTTP request, every Socket.IO frame), needing a restart.
        #
        # This stayed hidden while every preset used squelch 0, because rtl_fm
        # then streams FM hiss continuously and read() always returns promptly.
        # A squelched preset on a quiet channel emits nothing at all, so the
        # read blocks indefinitely and the deadlock is certain.
        #
        # Killing first makes read() return EOF, which releases the lock and lets
        # the close proceed.
        for spawned in sorted(self._spawned_pids):
            _kill_pid(spawned)
            if spawned == pid:
                log.info("rtl_fm stopped (pid %d)", spawned)
            else:
                log.warning("Reaped orphaned rtl_fm (pid %d) — it was holding the "
                            "SDR and would have blocked later tunes", spawned)
            self._spawned_pids.discard(spawned)

        if self._process:
            for pipe in (self._process.stdout, self._process.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass
        self._process = None
        self._pid = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._is_running = False
        self._drain_queues()

    def _poll_rtlfm(self):
        if self._process and self._process.poll() is not None:
            self._is_running = False
            return False
        return True

    def _read_loop(self):
        try:
            stdout = self._process.stdout
            while not self._stop_event.is_set():
                chunk = stdout.read(4096)
                if not chunk:
                    break
                try:
                    self.pcm_queue.put(chunk, timeout=0.5)
                except Exception:
                    pass
                try:
                    self.audio_queue.put_nowait(chunk)
                except Exception:
                    pass
        except (ValueError, OSError):
            pass
        self._is_running = False

    def _log_stderr(self):
        try:
            for line in self._process.stderr:
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    log.debug("rtl_fm: %s", msg)
        except (ValueError, OSError):
            pass

    def _drain_queues(self):
        for q in (self.pcm_queue, self.audio_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break
