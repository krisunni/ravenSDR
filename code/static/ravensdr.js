// ravenSDR frontend logic (Socket.IO, Web Audio, UI state)

(function () {
    "use strict";

    // ── State ──
    let currentPresetId = null;
    let presets = [];
    let categories = {};
    let activeCategory = null;
    let adsbEnabled = false;
    let mapVisible = false;

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

    // ── Socket.IO ──
    const socket = io();

    socket.on("connect", function () {
        connectionBanner.classList.add("hidden");
        fetchPresets();
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
        adsbEnabled = !!data.adsb_enabled;
    });

    socket.on("status", function (data) {
        updateStatus(data);
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
                    selectCategory(Object.keys(categories)[0]);
                }
            });
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

    function renderPresetButtons(catId) {
        presetButtons.innerHTML = "";
        var filtered = presets.filter(function (p) { return p.category === catId; });
        filtered.forEach(function (preset) {
            var btn = document.createElement("button");
            btn.className = "preset-btn";
            if (preset.id === currentPresetId) {
                btn.classList.add("active");
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

                // Manage ADS-B map panel based on preset + config
                var preset = data.preset || {};
                var isAviation = preset.category === "aviation";
                var isAdsbOnly = preset.mode === "adsb";

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
        }

        squelchSlider.value = data.squelch || 0;
        squelchValue.textContent = data.squelch || 0;
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
        });
    });

    gainSelect.addEventListener("change", function () {
        var value = gainSelect.value;
        fetch("/api/gain", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ value: value === "auto" ? "auto" : parseInt(value) }),
        });
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
