# ravenSDR

**Real-time RF Signal Transcription Pipeline**

RTL-SDR radio reception → Hailo-8L NPU inference → live web interface.

ravenSDR tunes into radio frequencies using a software-defined radio dongle, runs Whisper speech-to-text on a Hailo-8L neural processing unit, and streams both audio and transcripts to a browser-based console UI — all on a Raspberry Pi 5.

## Signal Flow

```
┌──────────┐    ┌────────────┐    ┌───────────┐    ┌───────────┐
│  RTL-SDR │───→│  16kHz PCM │───→│  Whisper   │───→│  Browser  │
│  / Web   │    │   Pipeline │    │  Hailo-8L  │    │  Console  │
│  Stream  │    │            │    │  NPU       │    │  UI       │
└──────────┘    └────────────┘    └───────────┘    └───────────┘
     FM/AM          Audio              AI            Transcripts
   Demod          Routing          Inference        + Live Audio
```

## Hardware

| Component | Model |
|-----------|-------|
| SBC | Raspberry Pi 5 |
| NPU | Hailo AI Hat (Hailo-8L, 13 TOPS) |
| SDR | RTL-SDR Blog V4 (R828D, 1PPM TCXO) |

## Features

- **Dual input modes** — SDR hardware (rtl_fm) or internet streams (ffmpeg), auto-detected at startup
- **Edge AI transcription** — Whisper-tiny on Hailo-8L NPU with automatic CPU fallback
- **Live audio streaming** — HTTP chunked WAV to browser via Web Audio API
- **Real-time transcripts** — Socket.IO push to browser as speech is decoded
- **Frequency presets** — Weather, aviation, marine, public safety, broadcast (Seattle/Redmond area)
- **Console UI** — Dark-themed single-page web app with signal meter, preset selector, transcript feed

## Quick Start

```bash
# On Raspberry Pi 5 with Hailo AI Hat + RTL-SDR
bash code/setup.sh
source .venv/bin/activate
pip install -r code/requirements.txt
python3 code/ravensdr/app.py
# Open http://localhost:5000
```

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
