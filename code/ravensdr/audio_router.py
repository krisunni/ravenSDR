# HTTP audio streaming — WAV header + chunked response

import struct
import logging
import time

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHANNELS = 1
BITS_PER_SAMPLE = 16
POLL_INTERVAL_S = 0.02   # 20ms; chunks arrive every ~128ms, so this is invisible
KEEPALIVE_AFTER_S = 5.0  # emit silence if the radio has produced nothing


def make_wav_header():
    """Create a WAV header for streaming (size set to 0xFFFFFFFF)."""
    byte_rate = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE // 8
    block_align = CHANNELS * BITS_PER_SAMPLE // 8
    max_size = 0xFFFFFFFF

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        max_size,           # file size (streaming — max)
        b"WAVE",
        b"fmt ",
        16,                 # chunk size
        1,                  # PCM format
        CHANNELS,
        SAMPLE_RATE,
        byte_rate,
        block_align,
        BITS_PER_SAMPLE,
        b"data",
        max_size,           # data size (streaming — max)
    )
    return header


def audio_stream_generator(audio_queue):
    """Yield a WAV header, then PCM chunks, forever.

    Polls rather than blocking. This runs in an eventlet greenthread while
    audio_queue is a REAL queue (see input_source), and a real get() would
    block the OS thread the hub runs on — freezing every other request for as
    long as the radio stays quiet. time.sleep is monkey-patched here, so it
    yields to the hub rather than sleeping the thread.

    This also removes the old failure mode: with a green queue the producer's
    notify never reached this consumer, so get(timeout=5) only ever returned on
    its own timeout and the browser received audio in 5-second bursts.
    """
    yield make_wav_header()
    idle = 0.0
    while True:
        try:
            chunk = audio_queue.get_nowait()
        except Exception:
            chunk = None

        if chunk is not None:
            idle = 0.0
            yield chunk
            continue

        time.sleep(POLL_INTERVAL_S)
        idle += POLL_INTERVAL_S
        if idle >= KEEPALIVE_AFTER_S:
            # Nothing for 5s — a squelched channel, or a stopped radio. Emit
            # silence so the browser keeps the connection instead of treating
            # a quiet band as a dead stream and reconnecting in a loop.
            idle = 0.0
            yield b"\x00" * 4096
