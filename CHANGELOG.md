# Changelog

## [1.3.0] — 2026-07-28

Console rebuilt around tabbed views; `/learn` reframed as a portfolio piece;
browser-based UI verification.

### Added

- **Signal classifier on the Hailo-8L NPU** — 5.2 ms median against 21.7 ms on the CPU (4.2×), at **97.5%** agreement with the validated ONNX model. Compiled from the custom ONNX through the Dataflow Compiler's Python API; `hailomz` handles Model Zoo models only.
- **Tabbed console** — twelve panels shared one vertical scroll, which put the signal classifier (where the node now spends most of its effort) below the fold and the decoders somewhere past that. Six views now: Listen, Classify, Decoders, Imagery, Science, Model. The preset selector stays above the tabs in every view, because tuning drives the radio regardless of what you are looking at.
- **Model view** — active backend, corpus size, collection state, and a per-class bar chart annotated with how many *distinct frequencies* each class was collected on, which is what decides whether a class can be validated at all.
- **View memory and follow-the-radio** — the active view persists in `localStorage`; an *explicit* tune switches to the view that preset feeds (ADS-B → Decoders, WEFAX → Imagery, meteor → Science). Page load deliberately does not, since it would override whatever the operator had open.
- **Tab badges** — live aircraft count on Decoders, last modulation on Classify, `REC` on Model while the collector is capturing.
- **Empty states** — a view whose panels are all hidden now explains which preset fills it instead of rendering blank.
- **`/learn` as a portfolio piece** — a hero stating what the node is, four figures read live from the running Pi, an "In short" summary above the tutorial (the strongest material was previously nine sections down), and a sticky section rail built from the DOM.
- **UI snapshot harness** (`code/scripts/ui_snapshot.py`) — renders the console and `/learn` in real Chromium at five viewport sizes, screenshots every view, and reports horizontal overflow, sub-11px text and sub-32px tap targets.

### Fixed

- **Corpus bars rendered at zero width**: `.mdl-bar-fill` is a `<span>` inside a non-flex parent, so it stayed inline and `width` was ignored — every bar measured 0px while reporting the correct percentage. Found by measuring in a browser, not by reading the CSS.
- **`/learn` panned sideways on phones** (153px at 390, 183px at 360): nine unconstrained wide tables dragging the document. Each now scrolls in its own container.
- **Console overflowed at 360px**: the C2 lamp groups and the classifier stat row each pushed the page 30–60px wider than the viewport.
- **Classifier backend displayed "None"** while a trained model was actively classifying — the `onnx` case was missing from the label map.
- **Model view "Band" showed the modulation class**, not the channel, repeating what the corpus bars already said. Now shows the frequency and preset id.
- **Loading the classifier onto the NPU evicted Whisper to the CPU.** Three components each created their own `VDevice`, but the Hailo-8L is one physical device and ROUND_ROBIN shares network groups only *within* a single VDevice — so whichever asked second got `HAILO_OUT_OF_PHYSICAL_DEVICES`. The error was logged, the console still said `hailo` for the classifier, and transcription just got slow. All three now share one VDevice.
- **`_infer_hailo` divided by 255** before submitting the buffer, but the compiled `.hef` takes `UINT8, NHWC` — normalisation is built into the graph. Float 0..1 would have been normalised twice, and the network would have seen near-black images while still returning confident labels.
- **The first `.hef` agreed with the CPU model only 53% of the time.** Not a wiring fault: it picked the right class but at 0.61 against 0.9985, so every borderline window flipped. Fifteen logits quantised to 8 bits leaves softmax too little resolution. The classifier head now runs at 16-bit with the conv trunk at 8-bit — 97.5%.
- **Corpus contamination — samples filed under a class the model does not have**: ten windows captured at 162.550 MHz (NOAA weather, narrowband FM) were written to an `ADSB` directory. `_collect_bands()` already excluded `mode=adsb` from the rotation, but `collect_label` was set on *every* tune, and ADS-B is map-only on the second dongle — so tuning it left a stale label in place while the main receiver sat elsewhere. AIS had the same shape. Both now clear the label, sharing one `NON_IQ_MODES` set with the rotation so the two rules cannot drift apart.
- **Garbled pager messages**: POCSAG numeric mode is BCD through multimon-ng's table `084 2.6]195-3U7[`, where `[` and `]` carry no meaning in paging. Payloads full of them are alphanumeric or binary content forced through the numeric table, or noise that survived BCH correction. These are now labelled rather than presented as messages — the raw payload is kept, since an undecodable page still proves the channel is live. Note that counting digits does **not** detect this: the observed noise is ~75% digits.

### Changed

- **Typography split** — monospace for data (frequencies, counts, callsigns), a UI sans for labels and prose. The whole document had been monospace.
- **Transcript feed** sized to content between bounds instead of a fixed 300px box that was mostly empty on a quiet channel.
- **Preset list** capped and internally scrollable on small screens; a category like Public Safety was 530px of buttons before you reached the tabs.
- **Section 10 of `/learn`** renamed from "Explaining this in an interview" to "Engineering decisions, and what they cost" — the content is the evidence, not notes about the evidence.

## [1.2.1] — 2026-07-27

The full machine-learning cycle, end to end on-node: collect a labelled corpus,
train off-node, run the result back on the Pi — and find out how much of the
headline accuracy was real.

### Added

- **Background IQ collection** (`iq_collector`, `iq_collect_scheduler`): rotates the dongle across bands to build a labelled training corpus, balanced **by class** rather than by frequency, with dwell weighted toward under-represented classes.
- **Preset-derived labels**: samples are labelled by whichever preset is tuned, so ground truth is free and never depends on the classifier's own output.
- **Trained model running on the Pi** via `onnxruntime` — the Hailo Dataflow Compiler is x86-only and behind a login, so the same network runs on the CPU at ~58 ms instead of a few. Backend order is Hailo → trained ONNX → heuristics.
- **Held-out-frequency validation** (`code/ml/signal_classifier/validate_confound.py`) — the test that decides whether a class is trustworthy.
- **Corpus pruning** (`prune_corpus.py`): applies the current signal-presence gate retroactively, quarantining rather than deleting so the judgement stays reviewable.
- **`/learn`** — a twelve-section animated explainer served from the node itself, including a stepped execution trace using values captured from a real run, and the network internals down to the convolution arithmetic. D3 is vendored, not from a CDN.
- **Adaptive VAD**: gates on level *above the measured noise floor* rather than a fixed RMS, so an open-squelch channel stops feeding static to the NPU.

### Fixed

- **99.1% accuracy was measuring the wrong thing.** Every class had been collected on exactly one frequency, so the network could score perfectly by learning the band and never the modulation. A random train/test split cannot detect this — both halves contain the same frequencies. Splitting by frequency and holding whole bands out of training dropped FM recall to 0.38. Three of six classes are now marked unproven in the UI, on purpose.
- **1,921 "OOK" samples were an empty channel** — 0% contained a burst, at only 2× the noise floor. Added a spectral peak-to-median gate.
- **"APRS" samples were a steady carrier** parked on 144.390, not packets: high spectral energy but a 3% burst rate, which is contradictory for a packet mode.
- **Half the corpus vanished silently** — 1,162 of 2,271 samples had preset modulations that were never in the class list, and the loader dropped them without a word.
- **`OOK/FSK` nested directory** orphaned 1,078 samples; ambiguous labels are now rejected at collection time.
- **OOM during validation**: 9,353 images as float32 × 3 channels is 5.6 GB on a 6 GB VM. Held as uint8 (0.47 GB) and expanded per batch.
- **The IQ rotation was poisoning its own corpus** by sampling bands whose protocol only transmits in bursts as though they were continuous.
- **Console needed the internet to show anything**: socket.io and Leaflet came from a CDN, so an air-gapped node rendered no panels at all. Both are now vendored.
- **Stale JS after deploys** — cache busting is now derived from file mtime.

### Changed

- The console no longer logs errors for ordinary states (an idle radio, an absent optional service).

## [1.2.0] — 2026-07-26

Split the console from the radio, and put a real arbiter in front of the dongle.

### Added

- **Two-process split**: `ui_app` and the radio process talk over a Unix socket with NDJSON framing, so the console loads and stays responsive even when the radio process is dead or restarting.
- **SDR arbiter** with command-and-control semantics — commanded vs actual state, LOCKED/SWITCHING/FAULT, request coalescing, and adoption of whatever the hardware is already doing. The header shows all of it.
- **Automation switch**: one toggle stops the satellite/WEFAX/ADS-B schedulers seizing the dongle.
- **APRS** packet decoding, additional ISM bands, and durable emitter history that survives restarts.

### Fixed

- **Orphaned `rtl_fm` holding the dongle**: overlapping tune requests raced because `_kill_pid` yields, letting one greenthread clear `self._pid` after another had stored a fresh one. The tuner now tracks every spawned pid and kills the whole set. An orphan had held the dongle for 17 minutes, breaking the pager decoder, a NOAA APT pass, and all tuning.
- **App-wide hub deadlock on a squelched tune**: `pipe.close()` blocked behind a reader still in `stdout.read()`. Processes are now killed *before* their pipes are closed.
- **Piped decoders misreported a busy dongle as a missing binary** — the source process's exit was never checked, so "is multimon-ng installed?" appeared when multimon-ng was fine and the dongle was not.
- **Cross-thread `greenlet.error` crashes**: eventlet had made the logging handler locks green, so real threads touching them exploded. Real `RLock`s plus an emit bridge that hands work to a green thread.
- **The console hijacked the radio on page load**, force-tuning a preset and destroying the saved `last_preset` so a restart could never resume. It now adopts the radio's existing state instead.

## [1.1.1] — 2026-07-23

NOAA APT satellite imagery — made it actually work end-to-end on the current
VHF dipole (137 MHz).

### Fixed

- **No passes predicted**: Celestrak's `weather` TLE group no longer carries the POES APT birds (it's JPSS/NOAA-20/21 now, which don't transmit APT). Switched the TLE source to `NAME=NOAA`, so NOAA 15/19 load and passes are predicted again.
- **APT recording** used the same fragile `rtl_fm | sox` pipe that failed for WEFAX (silent death → empty WAV). Replaced with a direct Python WAV writer that fails fast.

### Added

- **APT decoder** support via **aptdec** (lightweight C decoder; `setup.sh` builds it from source — noaa-apt needs Rust+GTK). `apt_decoder` auto-detects `aptdec`/`noaa-apt` and builds the right command, normalizing the output filename.

## [1.1.0] — 2026-07-23

Fresh Raspberry Pi 5 bring-up (Debian Trixie, kernel 6.18): repaired the Hailo NPU
backend, rebuilt WEFAX decoding, fixed ADS-B, and made NOAA weather parsing
accumulate across the broadcast loop.

### Fixed

- **Hailo NPU backend on 16 KB-page kernels** (Debian Trixie): the driver set the DMA descriptor page size to `PAGE_SIZE` (16384), over the Hailo-8 hardware max of 4096, so `InferModel.configure()` failed and Whisper silently fell back to CPU. Fixed with `/etc/modprobe.d/hailo_pci.conf` (`force_desc_page_size=4096 force_allocation_from_driver=1`), which also silences the `hailo_vdma_buffer_map` → `find_vma` kernel WARNING. Baked into `setup.sh`.
- **WEFAX produced no images**: the decode step invoked `fldigi --wefax-only` — not a real fldigi mode (fldigi has no headless WAV→PNG path). Replaced with a self-contained numpy decoder.
- **WEFAX HF reception (RTL-SDR Blog V4)**: dropped V3-style direct sampling (`-E direct2`) — the V4 has a built-in HF upconverter and tunes HF directly. Replaced the fragile `rtl_fm | sox` pipe (which died silently, writing empty files) with a direct Python WAV writer that fails fast.
- **ADS-B "Failed to start dump1090"**: no dump1090 was installed (Trixie ships none; apt `readsb` is built without RTL-SDR support). `setup.sh` now builds FlightAware dump1090 from source; `adsb_receiver` auto-detects the binary and retries startup to survive the USB-release race when switching audio↔ADS-B.
- **Weather panel "-1s ago"** clock-skew display bug (now clamps to "just now").

### Added

- **numpy WEFAX decoder** (`wefax_decode.py`): FM-demodulates the 1500/2300 Hz FSK, two-stage LPM deskew, phasing edge-align, grayscale PNG out — no fldigi/Xvfb/loopback. Verified by an encode→decode round-trip.
- **Manual WEFAX capture**: `POST /api/wefax/record` + a "Receive Now" button in the WEFAX tab (previously scheduler-only).
- **NOAA weather accumulator** (`WeatherAccumulator`): keeps a rolling window of transcripts and votes across NOAA's loop to extract **location (city)**, **current temperature** (median), **high/low**, **conditions**, **wind**, and **forecast** — robust to whisper-tiny garble that no single-chunk parse could handle.
- **Offline model pre-cache**: `download_models.sh --hf-cache` fetches the Whisper tokenizer + faster-whisper model for air-gapped operation.
- **Doctor script** (`scripts/debug.py`): end-to-end Hailo health check with an on-device inference smoke test.
- **`RAVENSDR_FORCE_BACKEND`** env var to pin the Whisper backend (`hailo`/`cpu`/`none`) and fail loudly instead of silently degrading.

### Changed

- **Removed PyTorch**: the mel spectrogram is now pure numpy (`mel.py`), numerically matched to the old torch implementation. Eliminates a large, wrong CUDA torch wheel from the Pi environment.
- **Dependencies aligned**: `pyproject.toml` now matches `requirements.txt` (transformers, huggingface_hub + hf_transfer, Pillow, ephem, pyais); numpy pinned `<2` for the pyhailort ABI.

## [1.0.0] — 2026-03-21

All 17 implementation phases complete. Full end-to-end IQ pipeline integrated.

### Added

- **Persistent config system** (`config.py` + `config.json`): secondary dongle assignment (meteor, ADS-B, or WEFAX) persists across restarts
- **Secondary dongle UI**: dropdown selector in header with status indicator, backed by `GET/POST /api/config/secondary`
- **Signal classifier panel** wired into main UI (`index.html`): always-visible when receiving audio
- **SEI emitter tracking panel** wired into main UI: always-visible when receiving audio
- **Version badge** in UI header from backend `VERSION` constant

### Changed

- **WEFAX receiver robustness**: 2-second startup health check for rtl_fm, finally-block cleanup prevents orphaned processes holding the USB device
- **WEFAX receiver multi-dongle**: `build_rtl_fm_cmd` now instance method using `self.device_index`
- **WEFAX scheduler**: expanded priority chart types (48hr, 96hr, 144hr forecasts, wave charts), ±60s trigger window (was ±10s), dedup set prevents double-triggers
- **InputSource**: `enter_apt_mode`/`enter_wefax_mode` always stop source to release USB, even if not marked running
- **Device contention**: meteor detector and ADS-B stopped before WEFAX recording in single-dongle mode
- **UI sections**: audio-related panels (signal, stats, classifier, SEI, controls) hidden on non-audio tabs

## [0.9.1] — 2026-03-17

### Changed — pyrtlsdr replaces rtl_fm as main audio pipeline

- **Dual-path Tuner** (`tuner.py`): auto-detects pyrtlsdr at startup. When available, all SDR operations use direct IQ capture via `IQCapture`, providing raw IQ for signal classification and spectrogram rendering while still feeding demodulated 16kHz PCM to Whisper and browser audio. Falls back to rtl_fm subprocess if pyrtlsdr is not installed — zero behavior change in fallback mode.
- **Python DSP pipeline** (`iq_capture.py`): FM demod (conjugate product frequency discriminator), AM demod (envelope detection), WFM demod, squelch gate (RMS thresholding, 0-100 range), de-emphasis filter (75μs single-pole IIR), frequency string parser (`"162.550M"` → Hz integer). Full Tuner-compatible interface (tune, stop, squelch, gain, sample rate, deemp, PPM, direct sampling).
- **Live IQ pipeline** (`app.py`): `IQSegmenter` processes every IQ chunk for transmission boundary detection, signal classifier runs every 500ms, `spectrogram_row` Socket.IO event emitted every 300ms for waterfall UI
- **Spectrogram waterfall live** (`classifier.js`): bound `spectrogram_row` event to existing `_renderWaterfall()` — spectrogram panel now renders live data from the SDR
- **IQ capture sample rate**: reduced from 2.4 MHz to 240 kHz (matches rtl_fm's 200k capture bandwidth, significantly reduces CPU load on Pi 5)

### Added

- `InputSource.set_iq_callback()` — passthrough for wiring raw IQ from Tuner to classifier/segmenter/SEI
- Environment variables `CLASSIFIER_HEF_PATH`, `CLASSIFIER_CLASSES_PATH`, `SEI_HEF_PATH` for NPU model deployment
- **NPU model training guide** (`operations/npu-model-training.md`): complete Mac ARM64 workflow for training, ONNX export, HEF compilation, and Pi deployment
- `setup.sh`: `pip install pyrtlsdr`, signal classifier and SEI data directories
- **Unit tests**: 34 new tests for DSP functions and IQCapture interface

### Unchanged

- Meteor detector, APT decoder, WEFAX receiver keep their independent rtl_fm instances (different sample rates, pipe to sox/fldigi/noaa-apt)
- Transcriber still reads 16kHz PCM from pcm_queue
- Audio router still streams WAV from audio_queue
- Web Stream mode (ffmpeg) unaffected

## [0.9.0] — 2026-03-17

### Added — Phase 16: Signal Classification via Spectrogram CNN

- **Signal classifier** (`signal_classifier.py`): real-time modulation identification from raw IQ samples using MobileNetV2 CNN on Hailo-8L NPU. Converts IQ to spectrogram images (256-point FFT, Hann window, 50% overlap), normalizes to 224x224 uint8, classifies into 11 target classes: AM, FM, WFM, SSB, P25, DMR, ADS-B, NOAA APT, WEFAX, CW, unknown
- **IQ capture** (`iq_capture.py`): direct IQ capture via pyrtlsdr bypassing rtl_fm, with FM/AM demodulation from raw IQ and device mutex to prevent concurrent access
- **CPU fallback classifier**: heuristic modulation classification using spectral features (bandwidth ratio, peak-to-average ratio) when Hailo NPU unavailable
- **Confidence thresholding**: only emit classification if top class > 0.7 confidence, flag uncertain when top two classes within 10%
- **Self-supervised labeling**: when classification matches preset `expected_modulation`, IQ chunk saved to `ml/signal_classifier/data/collected/` for retraining
- **ML training pipeline** (`code/ml/signal_classifier/`): RadioML 2018.01A dataset loader with custom class integration, MobileNetV2 fine-tuning with differential learning rates, ONNX export, Hailo DFC compilation for HAILO8L, model evaluation with confusion matrix
- **Classification panel UI** (`classifier.js` + `classifier.css`): always-visible panel with current modulation type display, confidence bar (color-coded), spectrogram waterfall canvas, scrolling classification history feed, accuracy tracker vs preset ground truth
- **Preset ground truth**: `expected_modulation` field added to all 19 frequency presets
- **REST endpoint**: `GET /api/classifier/status` (backend, total classifications, accuracy)
- **Socket.IO event**: `signal_classified` (modulation, confidence, frequency, timestamp, uncertain flag)
- **Unit tests**: 21 tests (IQ-to-spectrogram conversion, image normalization, confidence filtering, uncertainty detection, CPU fallback, status tracking, synthetic signals)
- **Integration test**: 7 tests (end-to-end classification, event emission, accuracy tracking, self-supervised logging, preset validation)

### Added — Phase 17: Specific Emitter Identification (SEI)

- **SEI model** (`sei_model.py`): passive RF fingerprinting via 1D CNN + attention on raw IQ samples, producing 128-dimensional L2-normalized embedding vectors. Cosine similarity matching (threshold 0.85) against local emitter database. Automatic enrollment of new emitters with sequential IDs (EMITTER-001, etc). Exponential moving average centroid updates on re-identification. Atomic JSON database persistence.
- **IQ segmenter** (`iq_segmenter.py`): transmission boundary detection from continuous IQ stream using power thresholding with hysteresis (100ms), ring buffer (10s), SNR estimation, duration filtering (50ms min, 30s max)
- **Classifier-to-SEI wiring**: `classify_segment()` method pipes classified transmission segments to SEI, `_forward_to_sei()` with confidence/SNR/duration gating (>0.7 confidence, >15 dB SNR, >100ms duration)
- **SEI ML training pipeline** (`code/ml/sei/`): ADS-B-labeled IQ collection script (ICAO hex ground truth), 1D CNN + SE attention model with triplet loss training, ONNX export, Hailo DFC compilation, rank-1/rank-5 accuracy evaluation
- **Emitter tracking panel UI** (`sei.js` + `sei.css`): always-visible panel with known emitters table (sortable, inline label editing via click), re-identification feed with ID/NEW badges, new emitter alert with pulse animation, database statistics bar
- **Emitter database** (`data/emitter_db.json`): JSON database with version tracking, sequential ID assignment, embedding centroids, frequency history, observation counts, user-assigned labels
- **REST endpoints**: `GET /api/emitters` (paginated), `GET /api/emitters/<id>`, `POST /api/emitters/<id>/label`, `GET /api/sei/status`
- **Socket.IO events**: `emitter_identified` (re-identification with confidence), `new_emitter` (enrollment notification)
- **Unit tests**: 10 IQ segmenter tests (power computation, boundary detection, hysteresis, SNR, duration filtering) + 22 SEI model tests (cosine similarity, L2 normalization, database CRUD, EMA updates, identification, status)
- **Integration test**: 7 tests (enrollment/re-identification, multi-emitter, persistence, API, labeling, classifier-to-SEI forwarding)

### Operational note

SEI operates in passive receive-only mode. No transmission occurs. All monitored signals are from third-party transmitters in publicly accessible spectrum. The system identifies hardware characteristics only — it does not decrypt encrypted signals.

## [0.7.0] — 2026-03-16

### Added — Phase 15: Passive Meteor Scatter Detection

- **Meteor detector** (`meteor_detector.py`): IQ power monitor with rolling baseline noise floor, configurable threshold (default 10 dB), duration-filtered burst detection (50ms–30s), underdense/overdense trail classification, JSON event logging
- **Meteor analyzer** (`meteor_analyzer.py`): shower calendar with 7 major showers (Quadrantids through Geminids), active shower correlation on detections, hourly/daily rate statistics, session stats with peak rate and trail type ratio
- **Shower calendar** (`data/meteor_showers.json`): annual meteor shower data with peak dates, active windows, ZHR, entry speeds, parent bodies, radiant coordinates
- **Dual dongle support**: `METEOR_DUAL_DONGLE=true` runs meteor detector on device 1 independently of main pipeline, single dongle mode runs at background priority
- **REST endpoints**: `GET /api/meteor/events` (paginated, filterable by shower/trail type), `GET /api/meteor/stats` (hourly rate, session stats, shower context), `GET /api/meteor/showers` (full calendar)
- **Socket.IO events**: `meteor_detection` (real-time per-burst), `meteor_stats_update` (60s interval)
- **Meteor panel UI** (`meteor.js` + `meteor.css`): real-time event feed with trail type badges (U/O), 24-hour rate bar chart on canvas, shower context display, statistics grid, target frequency info
- **InputSource meteor mode**: enter/exit with automatic preset restore, lowest priority (preempted by APT, WEFAX, voice monitoring)
- **Unit tests**: 47 tests for detector (threshold, duration filtering, trail classification, power measurement) and analyzer (shower calendar, rate calculation, event tagging)
- **Integration test**: mocked end-to-end pipeline, priority enforcement, dual dongle isolation

### Config (environment variables)

- `METEOR_ENABLED` — enable meteor scatter detection (default: `false`)
- `METEOR_DUAL_DONGLE` — use dedicated dongle on device 1 (default: `false`)
- `METEOR_FREQUENCY` — carrier frequency in Hz (default: `143050000` — amateur meteor scatter)

## [0.6.0] — 2026-03-16

### Added — Phase 14: WEFAX Weather Fax HF Reception

- **WEFAX scheduler** (`wefax_scheduler.py`): hardcoded NMC Point Reyes and NOJ Kodiak broadcast schedules with UTC times, time-of-day frequency selection (lower HF at night, higher during day), 5-minute advance notification, priority tagging for surface analysis and 24hr forecasts
- **WEFAX receiver** (`wefax_receiver.py`): rtl_fm HF direct sampling (`-D 2` Q-branch for V4 R828D), USB demodulation, 1.9 kHz frequency offset (WEFAX convention), fldigi decode via Xvfb headless, structured filename output (station, frequency, chart type, timestamp)
- **InputSource WEFAX mode**: enter/exit with automatic preset restore, APT satellite passes take priority over WEFAX windows
- **REST endpoints**: `GET /api/wefax/latest` (optional chart_type filter), `GET /api/wefax/schedule` (6-hour lookahead), `GET /api/wefax/history` (last 10 charts, filterable)
- **Socket.IO events**: `wefax_broadcast_upcoming` (5 min advance), `wefax_image_ready` (decode complete)
- **WEFAX panel UI** (`wefax.js` + `wefax.css`): broadcast schedule with station/frequency badges, countdown timer, active reception indicator, decoded chart display (click to expand), chart history thumbnails, chart type filter buttons
- **Setup**: fldigi and Xvfb install steps, WEFAX directory creation, HF antenna guidance
- **Unit tests**: 49 tests for scheduler (frequency selection, schedule parsing, callbacks) and receiver (command construction, frequency offset, filename parsing)
- **Integration test**: mocked pipeline, SDR mode transitions, APT priority enforcement

## [0.5.1] — 2026-03-03

### Fixed — Audio pipeline & transcription quality

- **Preset squelch not applied**: tuning to a preset ignored its squelch value (always defaulted to 0). Now applies preset squelch before starting rtl_fm
- **Transcription required audio playback**: audio queue `put(timeout=0.5)` blocked the read loop when nobody was listening, starving the transcriber. Changed to non-blocking `put_nowait()` so audio chunks are dropped silently when browser audio isn't playing
- **Audio stream breaks on squelch/gain change**: changing squelch or gain restarts rtl_fm, killing the HTTP audio stream. Frontend now auto-reconnects the stream if audio was playing
- **Whisper hallucination spam**: Whisper produced garbage transcripts on noise/static — `(roaring)`, `[Music]`, `[Groans]`, `[Birds]`, etc. Added two-tier hallucination filter: known phrases + structural pattern matching (bracketed sound descriptions, short fragments, repetitive syllables). All filtered transcripts logged at DEBUG level

### Added

- **Per-mode sample rate config**: `MODE_SAMPLE_RATES` dict in Tuner for mode-specific rtl_fm bandwidth (extensible for AM tuning)
- **Default startup preset**: UI now defaults to Weather tab and auto-tunes NOAA Seattle on page load
- **Cleanup script** (`scripts/cleanup.sh`): kills orphaned rtl_fm, dump1090, and ffmpeg processes

## [0.5.0] — 2026-03-02

### Fixed — RTL-SDR Blog V4 driver & Hailo NPU transcription

- **RTL-SDR Blog V4 driver**: stock Debian `librtlsdr` does NOT support the V4's R828D tuner, causing "PLL not locked" errors on every frequency. Setup script now builds the patched driver from `rtlsdrblog/rtl-sdr-blog` and reinstalls it after dump1090 (which pulls in the stock lib as a dependency)
- **Whisper decode prefix**: Hailo NPU decoder was seeded with only `<|startoftranscript|>`, causing immediate EOS (2/32 tokens). Now seeds with full Whisper prefix: `<|startoftranscript|> <|en|> <|transcribe|> <|notimestamps|>` — decoder now produces 28/32 tokens of real transcription
- **Signal meter flickering**: heartbeat loop emitted `rms: 0` every 500ms, overriding real signal values from the transcriber. Now only emits 0 on stop transition
- **ADS-B scan scheduler eventlet crash**: scheduler used `threading.Thread` causing "Cannot switch to a different thread" greenlet errors. Now uses `eventlet.spawn` and `eventlet.sleep`
- **ADS-B scans interrupting non-aviation presets**: scan scheduler now only activates when tuned to an Aviation preset
- **NOAA Weather presets**: changed from `wbfm` (wideband FM) to `fm` (narrowband) — NOAA Weather Radio is narrowband FM
- **Hailo detection in setup.sh**: `hailortcli fw-control identify` returns non-zero even on success; now also checks output text

### Added — Operations guides & UI improvements

- **Antenna guide** (`operations/antenna-guide.md`): element lengths per band, V-dipole orientation diagrams (vertical/horizontal/flat), positioning guidelines, troubleshooting (PLL fix, weak signal, USB power)
- **System diagram** (`operations/system-diagram.md`): physical setup, software architecture, data flow, ADS-B time-sharing, hardware stack, driver requirements
- **Squelch & Gain tooltips**: info icons with hover descriptions explaining what each control does
- **Satellite panel visibility**: now hidden by default, only shown on Weather tab
- **Audio auto-stop**: playback stops automatically when source stops
- **Setup script**: RTL-SDR Blog V4 driver build from source, dump1090 systemd service disabled (ravenSDR manages it), correct install order (dump1090 before Blog driver)

## [0.4.1] — 2026-03-02

### Fixed — Eventlet subprocess isolation & hardware integration bugs

- **Eventlet subprocess isolation**: tuner, stream_source, and input_source now use `eventlet.patcher.original("subprocess")` and `original("threading")` to get real stdlib modules. Eventlet's green subprocess caused fd conflicts ("Second simultaneous read on fileno"), broken `wait()` timeouts, and orphaned processes
- **NPU inference loop indentation**: mel spectrogram → encoder → decoder → emit block was outside the `for chunk in vad_segments:` loop, causing `UnboundLocalError` and immediate CPU fallback
- **Celestrak TLE URL**: changed to `gp.php?GROUP=weather&FORMAT=tle` (old path returned 404)
- **Shutdown crash**: `shutdown()` now spawns `_do_shutdown()` via `socketio.start_background_task()` to avoid `RuntimeError: do not call blocking functions from the mainloop`
- **SDR detection**: uses `lsusb` to check for RTL2838 USB ID (`0bda:2838`) — works even when dump1090 holds exclusive device access
- **Process cleanup**: tuner/stream_source `stop()` uses `os.kill()` + `os.waitpid()` directly, bypassing eventlet's broken `subprocess.wait()`
- **setup.sh rtl_test zombie**: `timeout --signal=KILL` prevents unkillable `rtl_test` from holding the dongle indefinitely; also stops dump1090 before SDR test
- **LiveATC stream headers**: added `User-Agent` and `Referer` headers to ffmpeg commands (LiveATC blocks headless requests)
- **Duplicate preset**: removed duplicate KUOW-FM entry from presets

## [0.3.0] — 2026-02-28

### Added — Phase 10: ADS-B Aviation Correlation & Voice-Activity Segmentation

- **ADS-B Receiver** (`adsb_receiver.py`): dump1090 process manager with JSON flight poller, single-dongle time-sharing and dual-dongle modes
- **Voice-Activity Segmenter**: silence-boundary audio chunking replaces fixed 10s chunks — no more mid-word splits. Configurable threshold, holdoff, min/max segment duration
- **Callsign Correlator** (`adsb_correlator.py`): regex extraction of airline callsigns (Alaska → ASA), ICAO codes (UAL), and N-numbers from Whisper transcripts, matched against live ADS-B flight list
- **Leaflet.js Map Panel**: real-time aircraft markers with directional icons, callsign tooltips, and 8-second highlight on transcript match
- **REST endpoint**: `GET /api/adsb/flights` returns current aircraft list
- **Socket.IO events**: `adsb_update` (flight list push every 2s), `callsign_match` (transcript correlation)
- **ADS-B 1090 MHz preset**: map-only tracking mode in Aviation category
- **Transcript callsign highlighting**: matched callsigns highlighted in red in the transcript feed
- **Setup**: dump1090-mutability install step in `setup.sh`, `requests` added to requirements.txt
- **Tests**: unit tests for callsign extraction, VAD segmenter, and integration tests for ADS-B receiver

### Changed

- ADS-B enabled by default (single-dongle mode). Set `ADSB_ENABLED=false` to disable
- Transcriber now uses `VoiceActivitySegmenter` in both Hailo NPU and CPU fallback paths
- Map panel auto-shows on Aviation presets when ADS-B is enabled, hidden otherwise

### Config (environment variables)

- `ADSB_ENABLED` — enable ADS-B receiver (default: `true`)
- `ADSB_DUAL_DONGLE` — use dedicated dongle on device 1 (default: `false`)
- `ADSB_SCAN_INTERVAL` — seconds between scan windows in single-dongle mode (default: `60`)
- `ADSB_SCAN_DURATION` — seconds per scan window (default: `30`)

## [0.2.0] — 2026-02-27

- Phases 1–9, 11 implemented
- Hailo-8L NPU inference, faster-whisper CPU fallback
- Flask + Socket.IO backend, vanilla JS frontend
- Frequency presets, error handling, inference stats dashboard
