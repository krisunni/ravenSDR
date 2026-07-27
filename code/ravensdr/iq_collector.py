# Raw IQ capture via rtl_sdr(1), for building a labelled training corpus.
#
# Why a subprocess rather than pyrtlsdr
# ------------------------------------
# The IQ pipeline (segmenter -> classifier -> SEI) only ever ran on the pyrtlsdr
# path, and pyrtlsdr cannot be used on this node: it fails at import against the
# RTL-SDR Blog driver with
#
#     AttributeError: librtlsdr.so: undefined symbol: rtlsdr_set_dithering
#
# and the Blog fork is required for the V4's R828D tuner, so "just install
# upstream librtlsdr" would trade IQ for the ability to receive at all. The
# tuner therefore runs rtl_fm, which emits demodulated AUDIO and no IQ — so the
# segmenter never received a sample and the corpus stayed empty.
#
# rtl_sdr(1) ships with the same Blog build and writes raw 8-bit IQ to stdout.
# Reading it as a subprocess mirrors what tuner.py already does with rtl_fm, and
# needs no Python bindings at all.
#
# Format: interleaved unsigned 8-bit I,Q pairs centred on 127.5.

import logging

import numpy as np

# Real stdlib, not eventlet's: this does a blocking read on a subprocess pipe
# from its own OS thread. See emit_bridge.py for why mixing the two is unsafe.
try:
    from eventlet.patcher import original
    subprocess = original("subprocess")
    threading = original("threading")
except ImportError:
    import subprocess
    import threading

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 2400000   # 2.4 MS/s — matches iq_segmenter's expectation
READ_CHUNK_BYTES = 65536        # 32768 IQ pairs per read
DC_OFFSET = 127.5               # unsigned 8-bit centre


def bytes_to_iq(raw):
    """Convert interleaved unsigned 8-bit I/Q bytes to a complex64 array."""
    samples = np.frombuffer(raw, dtype=np.uint8)
    if len(samples) < 2:
        return np.empty(0, dtype=np.complex64)
    if len(samples) % 2:
        samples = samples[:-1]          # drop a split pair rather than skew I/Q
    i = samples[0::2].astype(np.float32) - DC_OFFSET
    q = samples[1::2].astype(np.float32) - DC_OFFSET
    return (i + 1j * q).astype(np.complex64)


class IQCollector:
    """Runs rtl_sdr for a fixed dwell and streams IQ to a consumer."""

    def __init__(self, device_index=0, sample_rate=DEFAULT_SAMPLE_RATE,
                 on_iq=None, gain=None):
        self.device_index = device_index
        self.sample_rate = sample_rate
        self.on_iq = on_iq              # callback(iq_complex64, frequency_hz)
        self.gain = gain
        self._process = None
        self._thread = None
        self._running = False
        self._frequency_hz = 0
        self.bytes_read = 0

    @property
    def is_running(self):
        return self._running

    @property
    def frequency_hz(self):
        return self._frequency_hz

    def build_cmd(self, frequency_hz):
        cmd = ["rtl_sdr", "-f", str(int(frequency_hz)),
               "-s", str(int(self.sample_rate)),
               "-d", str(self.device_index)]
        if self.gain is not None:
            cmd += ["-g", str(self.gain)]
        return cmd + ["-"]              # stdout

    def start(self, frequency_hz):
        """Begin capturing. Returns True if rtl_sdr came up."""
        if self._running:
            log.warning("IQ collector already running")
            return False

        self._frequency_hz = int(frequency_hz)
        self.bytes_read = 0
        cmd = self.build_cmd(frequency_hz)
        log.info("IQ collector: %s", " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            log.error("rtl_sdr not found — cannot collect IQ")
            return False

        self._running = True
        self._thread = threading.Thread(target=self._read_loop,
                                        name="iq-collect", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        """Stop capturing and release the dongle."""
        self._running = False
        proc, self._process = self._process, None
        if proc is not None:
            # Terminate BEFORE closing the pipe: the reader thread holds
            # BufferedReader's lock while blocked in read(), so closing first
            # deadlocks whoever calls stop(). Same failure mode as tuner.py.
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            except OSError:
                pass
            try:
                if proc.stdout:
                    proc.stdout.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        log.info("IQ collector stopped (%.1f MB read)", self.bytes_read / 1e6)

    def _read_loop(self):
        proc = self._process
        if proc is None or proc.stdout is None:
            self._running = False
            return
        try:
            while self._running:
                raw = proc.stdout.read(READ_CHUNK_BYTES)
                if not raw:
                    break
                self.bytes_read += len(raw)
                if self.on_iq is None:
                    continue
                iq = bytes_to_iq(raw)
                if len(iq):
                    try:
                        self.on_iq(iq, self._frequency_hz)
                    except Exception:
                        log.exception("IQ consumer failed")
        except (OSError, ValueError):
            pass                        # pipe closed by stop()
        finally:
            self._running = False
