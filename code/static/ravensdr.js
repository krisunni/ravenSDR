// ravenSDR frontend logic (Socket.IO, Web Audio, UI state)

(function () {
    "use strict";

    // ── State ──
    let currentPresetId = null;
    let sdrC2 = null;          // last SDR command & control snapshot
    let presets = [];
    let categories = {};
    let activeCategory = null;
    let adsbEnabled = false;
    let mapVisible = false;
    let weatherPanel = null;
    let satellitePanel = null;
    let wefaxPanel = null;
    let meteorPanel = null;
    let classifierPanel = null;
    let seiPanel = null;
    let settingsPanel = null;
    let ismPanel = null;
    let acarsPanel = null;
    let pagerPanel = null;
    let aprsPanel = null;

    // ── DOM refs ──
    const modeBadge = document.getElementById("mode-badge");
    const connectionBanner = document.getElementById("connection-banner");
    const categoryTabs = document.getElementById("category-tabs");
    const presetButtons = document.getElementById("preset-buttons");
    const signalBar = document.getElementById("signal-bar");
    const signalRms = document.getElementById("signal-rms");
    const squelchSlider = document.getElementById("squelch-slider");
    const squelchValue = document.getElementById("squelch-value");
    const gainSelect = document.getElementById("gain-select");
    const stopBtn = document.getElementById("stop-btn");
    const audioToggle = document.getElementById("audio-toggle");
    const audioStatus = document.getElementById("audio-status");
    const audioPlayer = document.getElementById("audio-player");
    const tunedLabel = document.getElementById("tuned-label");
    const tunedFreq = document.getElementById("tuned-freq");
    const transcriptFeed = document.getElementById("transcript-feed");
    const clearBtn = document.getElementById("clear-btn");
    const copyBtn = document.getElementById("copy-btn");
    const advancedToggle = document.getElementById("advanced-toggle");
    const advancedPanel = document.getElementById("advanced-panel");
    const sampleRateSelect = document.getElementById("sample-rate-select");
    const deempToggle = document.getElementById("deemp-toggle");
    const deempLabel = document.getElementById("deemp-label");
    const ppmInput = document.getElementById("ppm-input");
    const directSamplingSelect = document.getElementById("direct-sampling-select");

    // ── Socket.IO ──
    const socket = io();

    socket.on("connect", function () {
        connectionBanner.classList.add("hidden");
        fetchPresets();
        fetchSecondaryConfig();
        if (window.WeatherPanel && !weatherPanel) {
            weatherPanel = new window.WeatherPanel(socket);
        }
        if (window.SatellitePanel && !satellitePanel) {
            satellitePanel = new window.SatellitePanel(socket);
        }
        if (window.WefaxPanel && !wefaxPanel) {
            wefaxPanel = new window.WefaxPanel(socket);
        }
        if (window.MeteorPanel && !meteorPanel) {
            meteorPanel = new window.MeteorPanel(socket);
        }
        if (window.ClassifierPanel && !classifierPanel) {
            classifierPanel = new window.ClassifierPanel(socket);
        }
        if (window.SEIPanel && !seiPanel) {
            seiPanel = new window.SEIPanel(socket);
        }
        if (window.SettingsPanel && !settingsPanel) {
            settingsPanel = new window.SettingsPanel(socket);
        }
        if (window.IsmPanel && !ismPanel) {
            ismPanel = new window.IsmPanel(socket);
        }
        if (window.AprsPanel) {
            aprsPanel = new window.AprsPanel(socket);
        }
        if (window.AcarsPanel && !acarsPanel) {
            acarsPanel = new window.AcarsPanel(socket);
        }
        if (window.PagerPanel && !pagerPanel) {
            pagerPanel = new window.PagerPanel(socket);
        }
    });

    // Keyword-hit toast: reuse the transcript feed as a lightweight notice
    socket.on("keyword_hit", function (data) {
        addNoticeEntry("KEYWORD [" + (data.severity || "info") + "] \"" + data.term
            + "\" — " + (data.transcript || ""));
    });

    socket.on("disconnect", function () {
        connectionBanner.textContent = "Disconnected — reconnecting...";
        connectionBanner.classList.remove("hidden");
    });

    socket.on("mode", function (data) {
        modeBadge.textContent = data.mode;
        modeBadge.className = "badge badge-" + data.mode.toLowerCase().replace(" ", "");
        if (data.transcriber_backend === "cpu") {
            modeBadge.textContent += " (CPU)";
        } else if (data.transcriber_backend === "none") {
            modeBadge.textContent += " (No Whisper)";
        }
        // Show version in header
        if (data.version) {
            var versionEl = document.getElementById("version-badge");
            if (versionEl) versionEl.textContent = "v" + data.version;
        }
        adsbEnabled = !!data.adsb_enabled;
        // Meteor panel shown via Science tab, not auto-show
    });

    socket.on("status", function (data) {
        updateStatus(data);
        if (data.sdr) renderSdrC2(data.sdr);
        if (data.automation) renderAutomation(data.automation);
    });

    socket.on("automation", function (data) {
        renderAutomation(data);
    });

    // SDR command & control: commanded vs actual, plus the transition.
    socket.on("sdr_state", function (data) {
        renderSdrC2(data);
    });

    // Link to the radio process. Distinguishes "the radio says nothing is
    // tuned" from "this console cannot reach the radio at all".
    socket.on("radio_link", function (data) {
        renderRadioLink(data);
    });

    socket.on("signal_level", function (data) {
        updateSignalMeter(data.rms);
    });

    socket.on("transcript", function (data) {
        addTranscriptEntry(data);
    });

    socket.on("inference_stats", function (stats) {
        updateStats(stats);
    });

    socket.on("notice", function (data) {
        addNoticeEntry(data.message);
    });

    socket.on("error", function (data) {
        addErrorEntry(data.message);
        if (data.recoverable) {
            showErrorBanner(data.message, data.type);
        }
        if (data.type === "sdr_reconnected") {
            hideErrorBanner();
        }
    });

    // ── Presets ──

    function fetchPresets() {
        fetch("/api/presets")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                presets = data.presets;
                categories = data.categories;
                renderCategoryTabs();
                if (Object.keys(categories).length > 0) {
                    adoptRadioState();
                }
            });
    }

    // Reflect what the radio is ALREADY doing; never impose a preset on load.
    //
    // This used to force-tune NOAA Seattle every time the page opened, which
    // yanked the dongle away from whatever was running and — because tuning
    // records last_preset — also destroyed the saved preset, so a restart could
    // never resume where the operator left off. The console is a view of the
    // hardware, not a command issued by opening a browser tab.
    function adoptRadioState() {
        var defaultCat = categories["weather"] ? "weather" : Object.keys(categories)[0];
        fetch("/api/sdr/state")
            .then(function (r) { return r.json(); })
            .then(function (snap) {
                var actual = snap && (snap.actual || snap.commanded);
                if (actual && actual.id) {
                    currentPresetId = actual.id;
                    selectCategory(actual.category || defaultCat);
                    if (snap) renderSdrC2(snap);
                    return;
                }
                // Radio is idle. Show a tab, but leave the hardware alone —
                // resuming on boot is the server's job (startup.auto_tune).
                selectCategory(defaultCat);
            })
            .catch(function () { selectCategory(defaultCat); });
    }

    function renderCategoryTabs() {
        categoryTabs.innerHTML = "";
        Object.keys(categories).forEach(function (catId) {
            var tab = document.createElement("button");
            tab.className = "tab";
            tab.textContent = categories[catId];
            tab.dataset.category = catId;
            tab.addEventListener("click", function () {
                selectCategory(catId);
            });
            categoryTabs.appendChild(tab);
        });
    }

    function selectCategory(catId) {
        activeCategory = catId;
        // Update active tab
        document.querySelectorAll(".tab").forEach(function (t) {
            t.classList.toggle("active", t.dataset.category === catId);
        });
        renderPresetButtons(catId);
    }

    // ── SDR command & control ──
    // The radio is separate hardware with a real switching delay, so the console
    // reports what it was COMMANDED to do, what it is ACTUALLY doing, and the
    // transition between the two.
    // ── Automation master switch ──
    // Reflects whether schedulers (satellite passes, WEFAX, ADS-B scan) are
    // allowed to seize the SDR. Paused means the radio only does what you ask.
    function renderAutomation(auto) {
        var box = document.getElementById("automation-enabled");
        var wrap = box && box.closest(".automation-config");
        var status = document.getElementById("automation-status");
        if (!box || !auto) return;
        var on = auto.enabled !== false;
        box.checked = on;
        if (wrap) wrap.classList.toggle("is-paused", !on);
        if (status) {
            status.textContent = on ? "" : "paused";
            status.title = on
                ? "Schedulers may take the SDR for satellite passes and WEFAX"
                : "Schedulers will not take the SDR; passes are still predicted";
        }
    }

    function wireAutomationToggle() {
        var box = document.getElementById("automation-enabled");
        if (!box) return;
        box.addEventListener("change", function () {
            fetch("/api/automation", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ enabled: box.checked }),
            })
                .then(function (r) { return r.json(); })
                .then(renderAutomation)
                .catch(function () { box.checked = !box.checked; });
        });
        fetch("/api/automation")
            .then(function (r) { return r.json(); })
            .then(renderAutomation)
            .catch(function () { /* radio may be down; leave default */ });
    }

    function renderSdrC2(snap) {
        if (!snap) return;
        const prev = sdrC2;
        sdrC2 = snap;

        const lamp = document.getElementById("c2-state-lamp");
        const stateEl = document.getElementById("c2-state");
        const actualEl = document.getElementById("c2-actual");
        const cmdEl = document.getElementById("c2-commanded");
        const faultEl = document.getElementById("c2-fault");
        if (!lamp || !stateEl) return;

        const state = snap.state || "LOCKED";
        const key = state.toLowerCase();
        lamp.className = "c2-lamp c2-lamp-" + key;
        stateEl.textContent = state;
        stateEl.className = "c2-state is-" + key;

        if (actualEl) actualEl.textContent = describeC2(snap.actual);
        if (cmdEl) {
            cmdEl.textContent = describeC2(snap.commanded);
            cmdEl.classList.toggle("is-pending", !!snap.in_transition);
        }

        if (faultEl) {
            if (state === "FAULT" && snap.last_error) {
                faultEl.textContent = snap.last_error;
                faultEl.classList.remove("hidden");
            } else {
                faultEl.classList.add("hidden");
            }
        }

        // Keep the preset grid's pending highlight in step with the transition.
        const wasPending = prev && prev.in_transition;
        const cmdChanged = !prev || describeC2(prev.commanded) !== describeC2(snap.commanded);
        if (activeCategory && (wasPending !== !!snap.in_transition || cmdChanged)) {
            renderPresetButtons(activeCategory);
        }
    }

    function renderRadioLink(snap) {
        const lamp = document.getElementById("c2-link-lamp");
        const label = document.getElementById("c2-link");
        if (!lamp || !label || !snap) return;
        const up = snap.link === "UP";
        lamp.className = "c2-lamp " + (up ? "c2-lamp-locked" : "c2-lamp-fault");
        label.textContent = up ? "LINK" : "NO LINK";
        label.className = "c2-state " + (up ? "is-locked" : "is-fault");
        label.title = up
            ? "Radio process connected (" + snap.socket + ")"
            : "Radio unreachable: " + (snap.last_error || "unknown");
        if (!up) {
            // Actual state is now unknowable — say so rather than showing stale data.
            const actualEl = document.getElementById("c2-actual");
            if (actualEl) actualEl.textContent = "—";
        }
    }

    function describeC2(entry) {
        if (!entry) return "—";
        const label = entry.label || entry.id || "?";
        return entry.freq ? label + "  " + entry.freq : label;
    }

    function renderPresetButtons(catId) {
        presetButtons.innerHTML = "";
        var filtered = presets.filter(function (p) { return p.category === catId; });
        filtered.forEach(function (preset) {
            var btn = document.createElement("button");
            btn.className = "preset-btn";
            if (preset.id === currentPresetId) {
                btn.classList.add("active");
            }
            // Switching takes ~1-2s. Flag the in-flight target so a click
            // visibly registers before the hardware has actually moved.
            if (sdrC2 && sdrC2.in_transition && sdrC2.commanded &&
                preset.id === sdrC2.commanded.id) {
                btn.classList.add("commanded");
            }
            // Grey out SDR-only presets in web stream mode
            if (modeBadge.textContent.indexOf("WEBSTREAM") !== -1 && !preset.stream_url) {
                btn.classList.add("disabled");
                btn.title = "SDR only — no web stream available";
            }
            btn.innerHTML = '<span class="preset-label">' + preset.label + '</span>' +
                '<span class="preset-freq">' + preset.freq + '</span>';
            btn.addEventListener("click", function () {
                tunePreset(preset.id);
            });
            presetButtons.appendChild(btn);
        });
    }

    function tunePreset(presetId) {
        var eb = document.getElementById("error-banner");
        if (eb) eb.classList.add("hidden");
        fetch("/api/tune", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ preset_id: presetId }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error) {
                    addErrorEntry(data.error);
                    return;
                }
                currentPresetId = presetId;
                renderPresetButtons(activeCategory);

                // Manage panels based on preset category
                var preset = data.preset || {};
                var isWeather = preset.category === "weather";
                var isWefax = preset.category === "wefax";
                var isScience = preset.category === "science";
                var isBroadcast = preset.category === "broadcast";
                if (weatherPanel) {
                    if (isWeather) {
                        weatherPanel.show();
                    } else {
                        weatherPanel.hide();
                    }
                }
                if (satellitePanel) {
                    if (isWeather) {
                        satellitePanel.show();
                    } else {
                        satellitePanel.hide();
                    }
                }
                if (wefaxPanel) {
                    if (isWefax) {
                        wefaxPanel.show();
                    } else {
                        wefaxPanel.hide();
                    }
                }
                if (meteorPanel) {
                    if (isScience) {
                        meteorPanel.show();
                    } else {
                        meteorPanel.hide();
                    }
                }
                // Refresh classifier panel status on tune
                if (classifierPanel) {
                    classifierPanel._fetchStatus();
                }

                var isIsm = preset.mode === "ism";
                if (ismPanel) {
                    if (isIsm) { ismPanel.show(); } else { ismPanel.hide(); }
                }
                var isAprs = preset.mode === "aprs";
                if (aprsPanel) {
                    if (isAprs) { aprsPanel.show(); } else { aprsPanel.hide(); }
                }
                var isAcars = preset.mode === "acars";
                if (acarsPanel) {
                    if (isAcars) { acarsPanel.show(); } else { acarsPanel.hide(); }
                }
                var isPager = preset.mode === "pager";
                if (pagerPanel) {
                    if (isPager) { pagerPanel.show(); } else { pagerPanel.hide(); }
                }

                // Manage map panel based on preset + config
                var isAviation = preset.category === "aviation";
                var isAdsbOnly = preset.mode === "adsb";
                var isAisOnly = preset.mode === "ais";
                var isMapMode = isAdsbOnly || isAisOnly;

                // Sections only relevant when actively receiving audio
                var audioSections = [
                    "signal-section", "stats-section", "classifier-panel",
                    "sei-panel", "control-section", "advanced-panel",
                    "audio-section", "tuned-section",
                ];
                var hasAudio = !isWefax && !isScience && !isAdsbOnly && !isAisOnly && !isIsm && !isAcars && !isPager;

                audioSections.forEach(function (id) {
                    var el = document.getElementById(id);
                    if (el) el.style.display = hasAudio ? "" : "none";
                });

                // WEFAX tab: show chart panel, hide transcript
                if (isWefax) {
                    hideMapPanel();
                    document.getElementById("transcript-section").style.display = "none";
                    return;
                }

                // Science tab: show meteor panel, hide transcript
                if (isScience) {
                    hideMapPanel();
                    document.getElementById("transcript-section").style.display = "none";
                    return;
                }

                // ISM tab: show sensor table, hide transcript + map
                if (isIsm) {
                    hideMapPanel();
                    document.getElementById("transcript-section").style.display = "none";
                    return;
                }

                // ACARS: show message feed, hide transcript + map (even though aviation)
                if (isAcars) {
                    hideMapPanel();
                    document.getElementById("transcript-section").style.display = "none";
                    return;
                }

                // Pager: show message feed, hide transcript + map
                if (isPager) {
                    hideMapPanel();
                    document.getElementById("transcript-section").style.display = "none";
                    return;
                }

                if (isAisOnly) {
                    showMapPanel(true);
                    document.getElementById("transcript-section").style.display = "none";
                    return;
                }

                if (!adsbEnabled || !isAviation) {
                    hideMapPanel();
                    document.getElementById("transcript-section").style.display = "";
                    return;
                }

                if (isAdsbOnly) {
                    showMapPanel(true);
                    document.getElementById("transcript-section").style.display = "none";
                } else {
                    showMapPanel(false);
                    document.getElementById("transcript-section").style.display = "";
                }
            });
    }

    // ── Status ──

    function updateStatus(data) {
        stopBtn.disabled = !data.running;
        audioToggle.disabled = !data.running;

        if (data.running) {
            tunedLabel.textContent = data.label || "Tuned";
            tunedFreq.textContent = data.freq || "";
            audioStatus.textContent = "Streaming";
        } else {
            tunedLabel.textContent = "Not tuned";
            tunedFreq.textContent = "";
            audioStatus.textContent = "No source";
            // Stop audio playback when source stops
            if (audioPlaying) {
                audioPlayer.pause();
                audioPlayer.removeAttribute("src");
                audioToggle.textContent = "Play Audio";
                audioPlaying = false;
            }
        }

        squelchSlider.value = data.squelch || 0;
        squelchValue.textContent = data.squelch || 0;

        // Sync advanced controls
        sampleRateSelect.value = data.sample_rate || "";
        ppmInput.value = data.ppm || 0;
        directSamplingSelect.value = data.direct_sampling || 0;

        // De-emphasis: reflect effective state
        deempToggle.checked = !!data.effective_deemp;
        if (data.deemp === null || data.deemp === undefined) {
            deempIsAuto = true;
            deempLabel.textContent = "Auto";
        } else {
            deempIsAuto = false;
            deempLabel.textContent = data.effective_deemp ? "ON" : "OFF";
        }
    }

    // ── Signal Meter ──

    function updateSignalMeter(rms) {
        var pct = Math.min(100, (rms / 10000) * 100);
        signalBar.style.width = pct + "%";
        signalRms.textContent = Math.round(rms);

        if (pct < 30) {
            signalBar.className = "meter-bar level-low";
        } else if (pct < 70) {
            signalBar.className = "meter-bar level-mid";
        } else {
            signalBar.className = "meter-bar level-high";
        }
    }

    // ── Inference Stats ──

    function updateStats(stats) {
        var backend = stats.backend === "hailo" ? "Hailo NPU" :
            stats.backend === "cpu" ? "CPU" : "None";
        document.getElementById("stat-backend").textContent = backend;

        document.getElementById("stat-latency").textContent =
            stats.last_total_ms > 0 ? stats.last_total_ms + " ms" : "\u2014";

        var rtfEl = document.getElementById("stat-rtf");
        if (stats.last_rtf > 0) {
            rtfEl.textContent = stats.last_rtf + "x";
            rtfEl.className = "stat-value" +
                (stats.last_rtf < 0.5 ? " stat-good" :
                 stats.last_rtf < 1.0 ? " stat-warn" : " stat-bad");
        } else {
            rtfEl.textContent = "\u2014";
            rtfEl.className = "stat-value";
        }

        document.getElementById("stat-tps").textContent =
            stats.last_tokens_per_sec > 0 ? stats.last_tokens_per_sec : "\u2014";

        document.getElementById("stat-decoder").textContent =
            stats.last_decoder_steps > 0
                ? stats.last_decoder_steps + "/" + stats.max_decoder_steps
                : "\u2014";

        document.getElementById("stat-chunks").textContent = stats.chunks_processed;

        var total = stats.chunks_processed + stats.chunks_skipped_silence;
        var silencePct = total > 0
            ? Math.round((stats.chunks_skipped_silence / total) * 100)
            : 0;
        document.getElementById("stat-silence").textContent = silencePct + "%";
    }

    // ── Transcript ──

    function addTranscriptEntry(data) {
        var entry = document.createElement("div");
        entry.className = "transcript-entry";
        entry.innerHTML =
            '<span class="ts">' + data.timestamp + '</span> ' +
            '<span class="freq">[' + data.label + ']</span> ' +
            '<span class="text">' + escapeHtml(data.text) + '</span>';
        transcriptFeed.appendChild(entry);
        transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
    }

    function addErrorEntry(message) {
        var entry = document.createElement("div");
        entry.className = "transcript-entry error-entry";
        entry.innerHTML =
            '<span class="ts">' + new Date().toLocaleTimeString() + '</span> ' +
            '<span class="error-text">ERROR: ' + escapeHtml(message) + '</span>';
        transcriptFeed.appendChild(entry);
        transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
    }

    function addNoticeEntry(message) {
        var entry = document.createElement("div");
        entry.className = "transcript-entry notice-entry";
        entry.innerHTML =
            '<span class="ts">' + new Date().toLocaleTimeString() + '</span> ' +
            '<span class="notice-text">' + escapeHtml(message) + '</span>';
        transcriptFeed.appendChild(entry);
        transcriptFeed.scrollTop = transcriptFeed.scrollHeight;
    }

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.textContent = text;
        return div.innerHTML;
    }

    // ── Controls ──

    stopBtn.addEventListener("click", function () {
        fetch("/api/stop", { method: "POST" })
            .then(function (r) { return r.json(); })
            .then(function () {
                currentPresetId = null;
                renderPresetButtons(activeCategory);
                audioPlayer.pause();
                audioPlayer.removeAttribute("src");
                audioToggle.textContent = "Play Audio";
                hideMapPanel();
                if (weatherPanel) weatherPanel.hide();
                if (satellitePanel) satellitePanel.hide();
                if (wefaxPanel) wefaxPanel.hide();
                if (meteorPanel) meteorPanel.hide();
                if (ismPanel) ismPanel.hide();
                if (aprsPanel) aprsPanel.hide();
                if (acarsPanel) acarsPanel.hide();
                if (pagerPanel) pagerPanel.hide();
                document.getElementById("transcript-section").style.display = "";
            });
    });

    squelchSlider.addEventListener("change", function () {
        var level = squelchSlider.value;
        squelchValue.textContent = level;
        fetch("/api/squelch", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ level: parseInt(level) }),
        }).then(reconnectAudio);
    });

    gainSelect.addEventListener("change", function () {
        var value = gainSelect.value;
        fetch("/api/gain", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value === "auto" ? "auto" : parseInt(value) }),
        }).then(reconnectAudio);
    });

    // ── Advanced Panel Toggle ──

    var advancedOpen = false;
    var deempIsAuto = true;

    advancedToggle.addEventListener("click", function () {
        advancedOpen = !advancedOpen;
        advancedPanel.classList.toggle("hidden", !advancedOpen);
        advancedToggle.textContent = advancedOpen ? "Advanced" : "Advanced";
        advancedToggle.classList.toggle("active", advancedOpen);
    });

    function reconnectAudio() {
        if (audioPlaying) {
            audioPlayer.src = "/audio-stream?" + Date.now();
            audioPlayer.play().catch(function () {});
        }
    }

    sampleRateSelect.addEventListener("change", function () {
        var value = sampleRateSelect.value || null;
        fetch("/api/sample_rate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value }),
        }).then(reconnectAudio);
    });

    deempToggle.addEventListener("change", function () {
        // Once user touches the toggle, switch from auto to manual
        deempIsAuto = false;
        var value = deempToggle.checked;
        deempLabel.textContent = value ? "ON" : "OFF";
        fetch("/api/deemp", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value }),
        }).then(reconnectAudio);
    });

    ppmInput.addEventListener("change", function () {
        var value = parseInt(ppmInput.value) || 0;
        ppmInput.value = value;
        fetch("/api/ppm", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value }),
        }).then(reconnectAudio);
    });

    directSamplingSelect.addEventListener("change", function () {
        var value = parseInt(directSamplingSelect.value);
        fetch("/api/direct_sampling", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value }),
        }).then(reconnectAudio);
    });

    // ── Audio Player ──

    var audioPlaying = false;

    audioToggle.addEventListener("click", function () {
        if (audioPlaying) {
            audioPlayer.pause();
            audioPlayer.removeAttribute("src");
            audioToggle.textContent = "Play Audio";
            audioPlaying = false;
        } else {
            audioPlayer.src = "/audio-stream?" + Date.now();
            audioPlayer.play().catch(function (e) {
                addErrorEntry("Audio play failed: " + e.message);
            });
            audioToggle.textContent = "Stop Audio";
            audioPlaying = true;
        }
    });

    // Auto-reconnect audio on error
    audioPlayer.addEventListener("error", function () {
        if (audioPlaying) {
            audioStatus.textContent = "Reconnecting audio...";
            setTimeout(function () {
                audioPlayer.src = "/audio-stream?" + Date.now();
                audioPlayer.play().catch(function () {});
            }, 2000);
        }
    });

    // ── Transcript controls ──

    clearBtn.addEventListener("click", function () {
        transcriptFeed.innerHTML = "";
    });

    copyBtn.addEventListener("click", function () {
        var entries = document.querySelectorAll(".transcript-entry:not(.error-entry)");
        var text = Array.prototype.map.call(entries, function (e) {
            return e.textContent;
        }).join("\n");
        navigator.clipboard.writeText(text).then(function () {
            copyBtn.textContent = "Copied!";
            setTimeout(function () { copyBtn.textContent = "Copy"; }, 1500);
        });
    });

    // ── Error Banner & Retry ──

    var errorBanner = document.getElementById("error-banner");
    var retryBtn = document.getElementById("retry-btn");

    function showErrorBanner(message, type) {
        errorBanner.querySelector(".error-message").textContent = message;
        errorBanner.classList.remove("hidden");
        retryBtn.classList.toggle("hidden", type === "sdr_disconnect");
    }

    function hideErrorBanner() {
        errorBanner.classList.add("hidden");
    }

    retryBtn.addEventListener("click", function () {
        retryBtn.disabled = true;
        retryBtn.textContent = "Retrying...";
        fetch("/api/retry", { method: "POST" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                retryBtn.disabled = false;
                retryBtn.textContent = "Retry";
                if (data.error) {
                    addErrorEntry(data.error);
                } else {
                    hideErrorBanner();
                }
            })
            .catch(function () {
                retryBtn.disabled = false;
                retryBtn.textContent = "Retry";
            });
    });

    // ── ADS-B Map ──

    socket.on("adsb_update", function (flights) {
        if (mapVisible && window.ravenMap) {
            window.ravenMap.updateAircraft(flights);
        }
    });

    socket.on("ais_update", function (vessels) {
        if (mapVisible && window.ravenMap) {
            window.ravenMap.updateVessels(vessels);
        }
    });

    socket.on("callsign_match", function (data) {
        if (mapVisible && window.ravenMap) {
            window.ravenMap.highlightAircraft(data.matches);
        }
        // Highlight callsigns in the most recent transcript entry
        highlightTranscriptCallsigns(data.matches);
    });

    function showMapPanel(fullWidth) {
        if (!window.ravenMap) return;
        window.ravenMap.init();
        window.ravenMap.show();
        window.ravenMap.setFullWidth(!!fullWidth);
        mapVisible = true;
    }

    function hideMapPanel() {
        if (window.ravenMap) {
            window.ravenMap.hide();
        }
        mapVisible = false;
    }

    // ── Automation switch ──
    wireAutomationToggle();

    // ── Secondary Dongle Config ──

    var secondarySelect = document.getElementById("secondary-select");
    var secondaryStatus = document.getElementById("secondary-status");

    function fetchSecondaryConfig() {
        fetch("/api/config/secondary")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (secondarySelect) {
                    secondarySelect.value = data.task || "";
                }
                if (secondaryStatus) {
                    secondaryStatus.className = "secondary-indicator" +
                        (data.running ? " active" : "");
                    secondaryStatus.title = data.running ?
                        (data.task + " running on device " + data.device_index) : "Off";
                }
            })
            .catch(function () {});
    }

    if (secondarySelect) {
        secondarySelect.addEventListener("change", function () {
            var task = secondarySelect.value || null;
            fetch("/api/config/secondary", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ task: task }),
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data.error) {
                        addErrorEntry(data.error);
                        return;
                    }
                    if (secondaryStatus) {
                        secondaryStatus.className = "secondary-indicator" +
                            (data.running ? " active" : "");
                    }
                })
                .catch(function () {});
        });
    }

    function highlightTranscriptCallsigns(matches) {
        // Highlight callsigns in the last transcript entry
        var entries = transcriptFeed.querySelectorAll(".transcript-entry:not(.error-entry)");
        if (entries.length === 0) return;
        var last = entries[entries.length - 1];
        var textSpan = last.querySelector(".text");
        if (!textSpan) return;

        matches.forEach(function (m) {
            var cs = m.matched_callsign || "";
            if (!cs) return;
            var html = textSpan.innerHTML;
            var regex = new RegExp("(" + cs.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
            textSpan.innerHTML = html.replace(regex,
                '<span class="callsign-match">$1</span>');
        });
    }

})();
