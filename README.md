# ravenSDR

**Real-time RF Signal Transcription Pipeline**

RTL-SDR radio reception → Hailo-8L NPU inference → live web interface.

ravenSDR tunes into radio frequencies using a software-defined radio dongle, runs Whisper speech-to-text on a Hailo-8L neural processing unit, and streams both audio and transcripts to a browser-based console UI — all on a Raspberry Pi 5.

## Signal Flow

One dongle, many protocols. What the RF becomes depends entirely on which
decoder reads the samples:

```
                      ┌──────────────────────────────────────────────┐
   ANTENNA ──► RTL-SDR │  tuner sets ONE 2.4 MHz window at a time     │
                      └───────────────────┬──────────────────────────┘
                                          │
        ┌─────────────────────────────────┼─────────────────────────────────┐
        │                                 │                                 │
   rtl_fm (audio)                  rtl_sdr (raw IQ)                 dedicated decoders
        │                                 │                                 │
        ▼                                 ▼                    dump1090 / rtl_433 / acarsdec
   adaptive VAD                    IQ segmenter                 multimon-ng / rtl_ais
        │                                 │                                 │
        ▼                                 ▼                                 ▼
   Whisper (Hailo NPU)          classifier + SEI              aircraft / sensors / packets
        │                                 │                                 │
        └─────────────────────────────────┴─────────────────────────────────┘
                                          │
                                   Flask + Socket.IO ──► browser console
```

Everything after the dongle is software. See [What "IQ" means](#what-iq-actually-is)
below if that flow looks like magic.

## Hardware

| Component | Model |
|-----------|-------|
| SBC | Raspberry Pi 5 |
| NPU | Hailo AI Hat (Hailo-8L, 13 TOPS) |
| SDR | RTL-SDR Blog V4 (R828D, 1PPM TCXO) |

## Features

**Voice + AI**
- **Edge transcription** — Whisper-tiny on the Hailo-8L NPU, automatic CPU fallback
- **Adaptive VAD** — gates on level *above the measured noise floor*, not a fixed
  threshold, so an open-squelch channel does not feed static to the NPU
- **Hallucination filtering** — drops Whisper's noise artifacts, and counts what it
  dropped so a working NPU never looks like a dead one
- **Keyword watchlist** — transcripts scanned for operator-defined terms

**Digital decoders** (same dongle, different software)
- **ADS-B** aircraft tracking with live map and ATC-callsign correlation
- **ACARS** aircraft messaging, correlated against tracked flights
- **APRS** packet — station positions, weather and telemetry
- **ISM / rtl_433** — weather stations, TPMS, utility meters, security sensors
- **POCSAG/FLEX** pager text
- **AIS** marine vessel tracking
- **NOAA APT** satellite imagery on scheduled passes
- **WEFAX** HF weather charts (needs an HF antenna)
- **Meteor scatter** detection

**Machine learning**
- **Signal classifier** — MobileNetV2 over IQ spectrograms (see [ML pipeline](#ml-pipeline))
- **SEI** — specific-emitter fingerprinting from raw IQ
- **Background corpus collection** — rotates bands to build labelled training data

**Operations**
- **Command & control UI** — commanded vs actual SDR state, with the transition visible
- **Automation switch** — one toggle stops schedulers seizing the dongle
- **Durable emitter history** — first/last seen and packet counts, surviving restarts
- **UI / radio process split** — the console loads even when the radio is dead

## Quick Start

```bash
# On Raspberry Pi 5 with Hailo AI Hat + RTL-SDR
bash code/setup.sh
source .venv/bin/activate
pip install -r code/requirements.txt
python3 code/ravensdr/app.py
# Open http://localhost:5000
```

## What "IQ" actually is

The dongle does not hand over "radio". It hands over an array of complex numbers
— two floats per sample, 2.4 million times a second:

```
dtype: complex64   I=22.50 Q=-26.50   ==   magnitude 34.76, angle -49.67 deg
```

A physical tuner still selects a centre frequency; what differs from an analog
radio is that it digitises a whole 2.4 MHz *window* and leaves every decision to
software. One capture at 94.9 MHz contains six FM stations, and "tuning" to any
of them afterwards is one multiply:

```python
moved = iq * np.exp(-2j * np.pi * offset * t)     # this IS tuning
```

Two numbers rather than one because `cos(+wt) == cos(-wt)`: a single real value
cannot tell a station above the centre from one below it. Modulation is then just
which part carries the data — AM the magnitude, FM the rate of angle change:

```python
audio = np.diff(np.unwrap(np.angle(iq)))          # FM demodulation, entire
```

## Hailo NPU usage

The Hailo-8L is a fixed-function inference accelerator: models must be compiled
ahead of time to `.hef` files. It cannot train.

| Workload | Status | Notes |
|---|---|---|
| Whisper tiny (encoder + decoder) | **running on NPU** | ~32 ms encode, RTF ~0.03-0.16 |
| Signal classifier (MobileNetV2) | CPU fallback | needs a compiled `.hef` |
| SEI emitter fingerprinting | CPU fallback | needs a compiled `.hef`, and Conv1d reshaped to Conv2d |

Notes that cost real debugging time:

- `InferModel` has **no `.infer()`** method. The API is
  `configure()` → `create_bindings()` → `set_buffer()` → `run()`.
- Every `VDevice` must be created with `HailoSchedulingAlgorithm.ROUND_ROBIN`,
  or the second model to start cannot acquire the device alongside Whisper.
- HailoRT's tensor layout is **NHWC**, while PyTorch/ONNX is NCHW — this applies
  to calibration data too, not just inference.
- Compile for `hailo8l`, not `hailo8`.
- On a 16 KB-page kernel, `configure()` needs `force_desc_page_size=4096`.

## ML pipeline

Training happens **off-node**: the Hailo Dataflow Compiler is x86-only and the Pi
is aarch64.

```
  Pi (collect)          x86 VM (train + compile)            Pi (infer)
  ------------          ------------------------            ----------
  rtl_sdr raw IQ   ──►  dataset.py   build spectrograms
  IQ segmenter          train.py     fine-tune MobileNetV2
  preset = label        export_onnx.py                 ──►  .hef on NPU
                        compile_hef.py  (Hailo DFC)
```

| Script | Purpose |
|---|---|
| `code/ml/signal_classifier/dataset.py` | IQ → spectrogram → 224x224 dataset |
| `code/ml/signal_classifier/train.py` | Fine-tune MobileNetV2 |
| `code/ml/signal_classifier/export_onnx.py` | PyTorch → ONNX |
| `code/ml/signal_classifier/compile_hef.py` | ONNX → `.hef` (needs x86 + DFC) |
| `code/ml/signal_classifier/evaluate.py` | Accuracy, per-class P/R/F1, confusion matrix |
| `code/ml/sei/*` | Same chain for the SEI embedding model |

Two constraints that decide whether a trained model is any good:

**Labels must be true.** The preset's declared modulation is the label — tuning
to 94.9 MHz means the samples *are* WFM. But collecting on a band the antenna
cannot hear files static under a real modulation, which is worse than having no
samples. HF bands are skipped for exactly this reason.

**Train and inference must see the same picture.** The runtime classifies ~24000-sample
windows; training on 1024-sample windows produced images with correlation 0.49
against the runtime's. `RUNTIME_SAMPLE_LEN` keeps them aligned.

## Stack

Python 3.11+ / Flask / Flask-SocketIO / Hailo SDK / faster-whisper / rtl-sdr / ffmpeg / Vanilla JS

## Project Dashboard

[View project status dashboard](https://krisunni.github.io/ravenSDR/dashboard/) — components, features, tasks, and changelog.

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

If you modify and deploy this software — including running it as a network service — you must make your source code available under the same license. See [LICENSE](LICENSE) for the full text.

## Attributions

ravenSDR is built on the work of many open-source projects. Full attributions, copyright notices, and license details for all dependencies are available in the [Attributions dashboard](https://krisunni.github.io/ravenSDR/dashboard/attributions.html).

Key dependencies and their licenses:

| Project | License | Used For |
|---------|---------|----------|
| [OpenAI Whisper](https://github.com/openai/whisper) | MIT | Speech-to-text model |
| [Hailo SDK](https://www.hailo.ai/) | Proprietary | NPU inference runtime |
| [hailort-drivers](https://github.com/hailo-ai/hailort-drivers) | Proprietary | Hailo kernel drivers |
| [rtl-sdr](https://github.com/osmocom/rtl-sdr) | GPL-2.0 | SDR driver (rtl_fm) |
| [FFmpeg](https://github.com/FFmpeg/FFmpeg) | LGPL-2.1+ | Web stream decoding |
| [Flask](https://github.com/pallets/flask) | BSD-3-Clause | Web framework |
| [Flask-SocketIO](https://github.com/miguelgrinberg/flask-socketio) | MIT | Real-time WebSocket |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | MIT | CPU inference fallback |
| [PyTorch](https://github.com/pytorch/pytorch) | BSD-3-Clause | Mel spectrogram computation |
| [Hugging Face Transformers](https://github.com/huggingface/transformers) | Apache-2.0 | Whisper tokenizer |
| [NumPy](https://github.com/numpy/numpy) | BSD-3-Clause | Audio signal processing |
| [Eventlet](https://github.com/eventlet/eventlet) | MIT | Async concurrency |
| [Socket.IO](https://github.com/socketio/socket.io-client) | MIT | Browser real-time comms |
