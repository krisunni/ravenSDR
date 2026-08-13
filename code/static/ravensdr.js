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
    var surveyPanel = null;
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
        loadTranslationSettings();
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
        // The !aprsPanel half was missing, so every reconnect built ANOTHER
        // panel that subscribed to aprs_update/aprs_packet and was never
        // released. After N drops, each 3s update triggered N+1 full table
        // rebuilds. Every other panel here was already guarded.
        if (window.AprsPanel && !aprsPanel) {
            aprsPanel = new window.AprsPanel(socket);
        }
        if (window.AcarsPanel && !acarsPanel) {
            acarsPanel = new window.AcarsPanel(socket);
        }
        if (window.SurveyPanel && !surveyPanel) {
            surveyPanel = new window.SurveyPanel(socket);
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

    // No modulation on the Classify tab. It changes several times a second and
    // strobed there; the label belongs in the panel, where there is room for the
    // confidence and frequency that make it mean anything.

    socket.on("signal_level", function (data) {
        updateSignalMeter(data.rms, data.excess_db);
        updateTranscribeStatus(data.segment);
    });

    socket.on("transcript", function (data) {
        addTranscriptEntry(data);
        flashTranscribed();
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
                    renderSdrC2(snap);
                    // The C2 snapshot carries only a brief preset view, so look
                    // up the full record — mode drives which tracker is shown.
                    var full = presets.find(function (p) { return p.id === actual.id; });
                    updatePanelsForPreset(full || actual);
                    refreshEmptyStates();
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

    // ── Tabbed workspace ──────────────────────────────────────────────────
    // Twelve panels stacked in one scroll meant the classifier — the thing the
    // node now spends most of its effort on — sat below the fold, and the
    // decoders were somewhere past that. Views group them; the last one is
    // remembered so a refresh does not dump you back at the top.
    var VIEW_KEY = "ravensdr.view";

    function showView(id) {
        document.querySelectorAll(".view").forEach(function (v) {
            v.classList.toggle("active", v.id === "view-" + id);
        });
        document.querySelectorAll(".view-tab").forEach(function (t) {
            t.classList.toggle("active", t.dataset.view === id);
        });
        try { localStorage.setItem(VIEW_KEY, id); } catch (e) {}
        if (id === "decoders" && window.ravenMap && mapVisible) {
            // Leaflet mis-measures itself if it was sized while hidden.
            setTimeout(function () { window.ravenMap.invalidateSize &&
                                     window.ravenMap.invalidateSize(); }, 60);
        }
        if (id === "model") refreshModelView();
        refreshEmptyStates();
    }

    // A view whose panels are all hidden (because the tuned preset does not
    // feed it) renders as a blank tab. Swap in an explanation instead.
    function refreshEmptyStates() {
        ["decoders", "imagery", "science"].forEach(function (v) {
            var view = document.getElementById("view-" + v);
            var empty = document.getElementById("empty-" + v);
            if (!view || !empty) return;
            var panels = view.querySelectorAll('div[id$="-panel"], section');
            var anyShown = false;
            for (var i = 0; i < panels.length; i++) {
                var el = panels[i];
                if (el === empty || empty.contains(el)) continue;
                // offsetParent is null for a hidden ancestor too, so read the
                // element's own display rather than its rendered box.
                if (getComputedStyle(el).display !== "none") { anyShown = true; break; }
            }
            empty.classList.toggle("hidden", anyShown);
        });
    }

    function wireViews() {
        var tabs = document.querySelectorAll(".view-tab");
        if (!tabs.length) return;
        tabs.forEach(function (t) {
            t.addEventListener("click", function () { showView(t.dataset.view); });
        });
        var saved = null;
        try { saved = localStorage.getItem(VIEW_KEY); } catch (e) {}
        showView(saved && document.getElementById("view-" + saved) ? saved : "listen");
    }

    // Badges: show activity without having to open the tab.
    function setBadge(id, text, cls) {
        var el = document.getElementById("badge-" + id);
        if (!el) return;
        el.textContent = text || "";
        el.className = "view-badge" + (cls ? " " + cls : "");
    }

    // ── Manual collection ──
    // The rotation is capped and only knows preset frequencies. This lets the
    // operator collect where they are actually tuned, which is the only way to
    // add the frequency diversity that validation needs.
    var collectWired = false;

    function wireCollectHere(classes) {
        var sel = document.getElementById("collect-label");
        var btn = document.getElementById("collect-go");
        var status = document.getElementById("collect-status");
        if (!sel || !btn) return;

        if (!sel.options.length && classes && classes.length) {
            classes.forEach(function (c) {
                var o = document.createElement("option");
                o.value = c; o.textContent = c;
                sel.appendChild(o);
            });
        }
        if (collectWired) return;
        collectWired = true;

        btn.addEventListener("click", function () {
            var count = parseInt(
                document.getElementById("collect-count").value, 10) || 300;
            status.textContent = "arming\u2026";
            status.className = "collect-status";
            btn.disabled = true;
            fetch("/api/collect-here", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({label: sel.value, count: count})
            }).then(function (r) {
                return r.json().then(function (d) { return {ok: r.ok, d: d}; });
            }).then(function (res) {
                btn.disabled = false;
                if (!res.ok) {
                    status.textContent = res.d.error || "failed";
                    status.className = "collect-status err";
                    return;
                }
                status.textContent = "collecting " + res.d.armed + " as " +
                    res.d.label + " on " + (res.d.freq || "current frequency");
                status.className = "collect-status ok";
            }).catch(function () {
                btn.disabled = false;
                status.textContent = "request failed";
                status.className = "collect-status err";
            });
        });
    }

    function refreshModelView() {
        fetch("/api/classifier/status").then(function (r) { return r.json(); })
          .then(function (d) {
              var set = function (id, v, cls) {
                  var el = document.getElementById(id);
                  if (!el) return;
                  el.textContent = v;
                  if (cls) el.className = cls;
              };
              var labels = { hailo: "Hailo NPU", onnx: "Trained model (CPU)",
                             cpu: "Heuristic rules", none: "None" };
              set("mdl-backend", labels[d.backend] || d.backend);
              set("mdl-arch", d.model || "—");
              set("mdl-total", (d.classifications_total || 0).toLocaleString());
              set("mdl-acc", Math.round((d.accuracy_vs_presets || 0) * 100) + "%"
                             + " (" + (d.correct_count || 0) + "/" + (d.compared_count || 0) + ")");
              set("mdl-validated", (d.validated_classes || []).join(" ") || "none", "ok");
              set("mdl-unproven", (d.unproven_classes || []).join(" ") || "none", "warn");
              window._unproven = d.unproven_classes || [];
              wireCollectHere(Object.keys(d.validation || {}).sort());
          }).catch(function () {});

        fetch("/api/iq-collect").then(function (r) { return r.json(); })
          .then(function (d) {
              var c = d.corpus || {}, band = d.current_band || {};
              var set = function (id, v) {
                  var el = document.getElementById(id); if (el) el.textContent = v;
              };
              set("mdl-collecting", d.capturing ? "capturing"
                                   : (d.running ? "idle between bands" : "stopped"));
              // band.label is the MODULATION class, not the channel — showing
              // it here just repeated what the corpus bars already say. The
              // useful answer is which frequency is being recorded right now.
              set("mdl-band", band && band.id
                  ? (band.freq_hz ? (band.freq_hz / 1e6).toFixed(3) + " MHz  " : "")
                    + band.id
                  : "— (" + (d.rotations || 0) + " rotations done)");
              set("mdl-corpus", (c.total || 0).toLocaleString());
              set("mdl-empty", ((c.skipped_empty || 0) +
                                (c.skipped_low_snr || 0)).toLocaleString());
              // A burst in flight is the operator's own request — show it
              // finishing rather than leaving the button looking inert.
              var st = document.getElementById("collect-status");
              if (st && c.burst_remaining) {
                  st.textContent = c.burst_remaining + " samples remaining";
                  st.className = "collect-status ok";
              }
              renderCorpusBars(c.per_class || {}, d.frequencies_per_class || {});
              setBadge("model", d.capturing ? "REC" : "", d.capturing ? "live" : "");
          }).catch(function () {});
    }

    function renderCorpusBars(perClass, perClassFreqs) {
        var wrap = document.getElementById("mdl-corpus-bars");
        if (!wrap) return;
        var rows = Object.keys(perClass).map(function (k) {
            return { k: k, v: perClass[k] };
        }).sort(function (a, b) { return b.v - a.v; });
        wrap.innerHTML = "";
        if (!rows.length) { wrap.innerHTML = "<div class='mdl-note'>No samples yet.</div>"; return; }
        var max = rows[0].v;
        var unproven = window._unproven || [];
        rows.forEach(function (r) {
            var nf = (perClassFreqs || {})[r.k] || 0;
            // Amber = the class was only ever heard on one frequency, so a high
            // score cannot be distinguished from the model memorising that band.
            var bad = unproven.indexOf(r.k) !== -1;
            var row = document.createElement("div");
            row.className = "mdl-bar-row";
            row.innerHTML =
                '<span class="mdl-bar-label">' + r.k + '</span>' +
                '<span class="mdl-bar-track"><span class="mdl-bar-fill' +
                    (bad ? " unproven" : "") + '" style="width:' +
                    Math.max(2, 100 * r.v / max) + '%"></span></span>' +
                '<span class="mdl-bar-count">' + r.v + '</span>' +
                '<span class="mdl-bar-freqs" title="distinct frequencies">' +
                    nf + (nf === 1 ? " freq" : " freqs") + '</span>';
            wrap.appendChild(row);
        });
        var note = document.getElementById("mdl-balance");
        if (note) {
            var min = rows[rows.length - 1].v;
            var ratio = min ? max / min : 0;
            note.innerHTML = "Largest class is <b>" + ratio.toFixed(1) +
                "&times;</b> the smallest. " + (ratio > 4
                ? "Too skewed to train on — the model would learn to guess the majority."
                : "Workable balance.") +
                " Amber bars are classes seen on only one frequency.";
        }
    }

    // ── Automation master switch ──
    // Two independent claims on one dongle, each with its own switch:
    //   Sched   — schedulers (satellite passes, WEFAX, ADS-B scan) may seize it
    //   Collect — background IQ collection, the only live feed for the waterfall
    // They were conflated behind one box, so ticking "Auto" appeared to do
    // nothing for anyone chasing a blank spectrum.
    var AUTO_SWITCHES = [
        {
            key: "enabled",
            box: "automation-enabled",
            label: "sched-switch",
            status: "automation-status",
            offClass: "is-paused",
            offText: "paused",
            onTitle: "Schedulers may take the SDR for satellite passes and WEFAX",
            offTitle: "Schedulers will not take the SDR; passes are still predicted",
            defaultOn: true,
        },
        {
            key: "iq_collect",
            box: "automation-iq-collect",
            label: "collect-switch",
            status: "iq-collect-status",
            offClass: "is-off",
            offText: "off",
            onTitle: "Collecting IQ in the background — the spectrum waterfall is live, "
                   + "and audio stops during each dwell",
            offTitle: "No IQ is captured, so the spectrum waterfall stays blank. "
                    + "Audio is never interrupted.",
            defaultOn: false,
            // Suppressed by the master switch server-side; say so rather than
            // showing a ticked box for work that is not happening.
            gatedByMaster: true,
        },
    ];

    function renderAutomation(auto) {
        if (!auto) return;
        // The master switch wins server-side (config.is_automation_enabled
        // returns false for every task when `enabled` is off). A ticked Collect
        // box under a disabled master therefore reads as "collecting" while
        // nothing is — the exact confusion the split toggles were meant to end.
        var masterOff = auto.enabled === false;
        AUTO_SWITCHES.forEach(function (s) {
            var box = document.getElementById(s.box);
            if (!box) return;
            // Absent key means the server never sent it; fall back to the
            // shipped default rather than silently reading it as "off".
            var on = auto[s.key] === undefined ? s.defaultOn : auto[s.key] !== false;
            box.checked = on;

            var gated = s.gatedByMaster && masterOff && on;
            var label = document.getElementById(s.label);
            if (label) label.classList.toggle(s.offClass, !on || gated);
            var status = document.getElementById(s.status);
            if (status) {
                status.textContent = gated ? "blocked" : (on ? "" : s.offText);
                status.title = gated
                    ? "Ticked, but Sched is off and the master switch disables "
                      + "every task — nothing is being collected."
                    : (on ? s.onTitle : s.offTitle);
            }
        });
    }

    function wireAutomationToggle() {
        AUTO_SWITCHES.forEach(function (s) {
            var box = document.getElementById(s.box);
            if (!box) return;
            box.addEventListener("change", function () {
                var patch = {};
                patch[s.key] = box.checked;
                fetch("/api/automation", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(patch),
                })
                    .then(function (r) { return r.json(); })
                    .then(renderAutomation)
                    .catch(function () { box.checked = !box.checked; });
            });
        });
        fetch("/api/automation")
            .then(function (r) { return r.json(); })
            .then(renderAutomation)
            .catch(function () { /* radio may be down; leave default */ });
    }


    // ── Radio activity ──
    // One dongle, three kinds of claimant, and only one of them is a person.
    // Without saying which has it, an idle audio path and an empty transcript
    // look identical to a broken node — which is exactly how this was reported.
    var RADIO_ACTIVITY = {
        operator:   { icon: "\u25B6", cls: "ra-operator" },
        background: { icon: "\u25C9", cls: "ra-background" },
        scheduled:  { icon: "\u23F1", cls: "ra-scheduled" },
        idle:       { icon: "\u25CB", cls: "ra-idle" }
    };

    function renderRadioActivity(d) {
        var host = document.getElementById("view-listen");
        if (!host || !d) return;
        var el = document.getElementById("radio-activity");
        if (!el) {
            el = document.createElement("div");
            el.id = "radio-activity";
            host.insertBefore(el, host.firstChild);
        }
        var kind = RADIO_ACTIVITY[d.who] || RADIO_ACTIVITY.idle;
        el.className = "radio-activity " + kind.cls;

        var extra = "";
        if (d.who === "background") {
            extra = "Tune any preset and it hands the radio back within a second.";
        } else if (d.who === "operator" && d.collect_blocked_by === "operator") {
            extra = "Corpus collection is paused" +
                (d.lease_remaining_s ? ", resuming in " + fmtMMSS(d.lease_remaining_s) : "") +
                ". Any tune or squelch change extends this.";
        } else if (d.who === "scheduled") {
            extra = "A pass happens now or not at all, so it takes priority.";
        } else if (d.who === "idle" && d.collect_enabled === false) {
            extra = "Background collection is off — tick “Collect” in the header. "
                  + "It is also the only live source for the spectrum waterfall.";
        }

        el.innerHTML =
            '<span class="ra-icon">' + kind.icon + '</span>' +
            '<span class="ra-text"><b>' + escapeHtml(d.detail || "") + '</b>' +
            (extra ? '<span class="ra-extra">' + extra + '</span>' : '') +
            '</span>';
    }

    function fmtMMSS(sec) {
        var m = Math.floor(sec / 60), s2 = Math.round(sec % 60);
        return m + ":" + (s2 < 10 ? "0" : "") + s2;
    }

    function pollRadioActivity() {
        fetch("/api/radio-activity")
            .then(function (r) { return r.json(); })
            .then(renderRadioActivity)
            .catch(function () {});
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

        if (actualEl) {
            actualEl.textContent = describeC2(snap.actual);
            // The corpus collector takes the dongle on a rotation. Without
            // saying so, the operator sees their preset in CMD, a dead audio
            // path, and an empty transcript, with nothing connecting the three.
            actualEl.classList.toggle("is-collecting",
                !!(snap.actual && snap.actual.collecting));
            // The radio-activity strip explains the rest; refresh it now so the
            // banner turns over in the same frame as the C2 lamp.
            pollRadioActivity();
        }
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

    // Show/hide the panels belonging to a preset.
    //
    // Extracted from the tune response handler so it can ALSO run when the
    // console adopts hardware state on load. It previously ran only on an
    // explicit tune, so opening the page while the radio was already on an
    // ISM/APRS/ACARS preset picked the right tab but never showed the tracker
    // table — the panel stayed hidden until you re-clicked the preset.
    function updatePanelsForPreset(preset) {
        preset = preset || {};
        // Manage panels based on preset category
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
            "signal-section", "stats-section",
            "control-section", "advanced-panel",
            "audio-section", "tuned-section",
            // The strip wrapping the first three; without it an empty grid
            // still contributes its margin on decoder-only presets.
            "listen-strip",
        ];
        var hasAudio = !isWefax && !isScience && !isAdsbOnly && !isAisOnly && !isIsm && !isAcars && !isPager;

        audioSections.forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.style.display = hasAudio ? "" : "none";
        });

        // The classifier and SEI work on IQ, not audio, so they must NOT be
        // hidden with the audio controls. Their only source of live IQ on this
        // node is the background collector, which runs on ISM/APRS/packet
        // presets — exactly the ones "hasAudio" switches off. Hiding them there
        // meant the panels were invisible precisely when they had data.
        ["classifier-panel", "sei-panel"].forEach(function (id) {
            var el = document.getElementById(id);
            if (el) el.style.display = "";
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
    }

    // Tuning a decoder preset while sitting on another tab used to look like
    // nothing happened — the data was rendering in a view you were not on.
    // Follow the radio to wherever its output actually lands.
    var MODE_VIEW = {
        adsb: "decoders", ais: "decoders", ism: "decoders",
        aprs: "decoders", acars: "decoders", pager: "decoders",
        wefax: "imagery", apt: "imagery", satellite: "imagery",
        // weather presets carry the NOAA APT decode, which renders in Imagery.
        meteor: "science",
    };

    function followPresetToView(preset) {
        if (!preset) return;
        var byCategory = { science: "science", wefax: "imagery", weather: "imagery" };
        var target = MODE_VIEW[preset.mode] || byCategory[preset.category] || "listen";
        if (!document.getElementById("view-" + target)) return;
        showView(target);
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

                updatePanelsForPreset(data.preset || {});
                refreshEmptyStates();
                // Only on an EXPLICIT tune. Doing this on page load would
                // override the view the operator had open when they refreshed.
                followPresetToView(data.preset || {});
            });
    }

    // ── Status ──

    function updateStatus(data) {
        setTranscribeRunning(data.running);
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

    // Colour by how far the audio sits ABOVE the measured noise floor, not by
    // raw level. The old thresholds were audio-clipping semantics — green quiet,
    // red loud — which is backwards for a receiver AND actively misleading on
    // FM: an absent carrier demodulates to full-scale hiss, so a dead channel
    // pegged the meter red as though it were a strong signal. Measured on a
    // silent NOAA channel: 8,488 RMS, 85% of scale, nothing transmitting.
    //
    // Excess over the floor answers the question the meter is actually for:
    // is there anything here?
    function updateSignalMeter(rms, excessDb) {
        var pct = Math.min(100, (rms / 10000) * 100);
        signalBar.style.width = pct + "%";
        signalRms.textContent = Math.round(rms);

        var cls, title;
        if (excessDb === null || excessDb === undefined) {
            // Floor still settling — say nothing rather than guess.
            cls = "level-unknown";
            title = "Measuring the noise floor\u2026";
        } else if (excessDb >= 10) {
            cls = "level-signal";
            title = "Signal " + excessDb.toFixed(1) + " dB above the noise floor";
        } else if (excessDb >= 3) {
            cls = "level-marginal";
            title = "Marginal — only " + excessDb.toFixed(1) + " dB over the floor";
        } else {
            cls = "level-noise";
            title = "Noise only (" + excessDb.toFixed(1) +
                " dB over the floor) — nothing is transmitting here";
        }
        signalBar.className = "meter-bar " + cls;
        var wrap = signalBar.parentElement;
        if (wrap) wrap.title = title;
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

    // ── Transcription status ────────────────────────────────────────────
    // Transcription does NOT depend on the audio stream — the tuner feeds the
    // transcriber and the browser from two separate queues. But a continuous
    // broadcast uses fixed 30s segments, so after tuning there is a full half
    // minute of empty panel before the first line lands, and that reads as
    // broken. This says "working, N seconds in" for that whole window.
    var tsEl = document.getElementById("transcribe-status");
    var tsLabel = tsEl && tsEl.querySelector(".ts-label");
    var tsBar = tsEl && tsEl.querySelector(".ts-bar i");
    var tsFlashUntil = 0;

    function updateTranscribeStatus(seg) {
        if (!tsEl) return;
        tsLastSignal = Date.now();
        if (!seg) {                       // no transcriber, or a mode without one
            tsEl.className = "ts-status idle";
            if (tsLabel) tsLabel.textContent = "not transcribing";
            if (tsBar) tsBar.style.width = "0%";
            return;
        }
        if (Date.now() < tsFlashUntil) return;   // let the "transcribed" flash sit

        var pct = seg.target_s ? Math.min(100, (seg.buffered_s / seg.target_s) * 100) : 0;
        tsEl.className = "ts-status live";
        if (tsBar) tsBar.style.width = pct.toFixed(0) + "%";
        if (tsLabel) {
            // The two segmenters mean different things by "target", so they get
            // different wording: continuous counts DOWN to a fixed cut, VAD just
            // accumulates until the talker stops.
            tsLabel.textContent = seg.mode === "continuous"
                ? "segment " + Math.max(0, seg.target_s - seg.buffered_s).toFixed(0) + "s"
                : (seg.buffered_s > 0.2 ? "capturing " + seg.buffered_s.toFixed(0) + "s" : "listening");
        }
    }

    // A squelched preset (every ham and public-safety channel here) produces NO
    // audio at all while the channel is quiet — rtl_fm emits nothing, so there
    // are no signal_level events and nothing above ever runs. Without this the
    // indicator would sit at its initial dash forever on exactly the presets
    // where "is this thing working?" matters most. Silence is a state, so say so.
    var tsLastSignal = 0;
    var tsRunning = false;

    function setTranscribeRunning(running) {
        tsRunning = !!running;
        if (!tsRunning) tsIdleCheck();
    }

    function tsIdleCheck() {
        if (!tsEl) return;
        if (Date.now() < tsFlashUntil) return;
        var quietFor = Date.now() - tsLastSignal;
        if (!tsRunning) {
            tsEl.className = "ts-status idle";
            if (tsLabel) tsLabel.textContent = "radio stopped";
            if (tsBar) tsBar.style.width = "0%";
        } else if (quietFor > 3000) {
            // Armed and waiting. Distinct wording from "radio stopped" because
            // the operator's next action differs: nothing, vs. go tune something.
            tsEl.className = "ts-status idle";
            if (tsLabel) tsLabel.textContent = "squelch closed";
            if (tsBar) tsBar.style.width = "0%";
        }
    }
    setInterval(tsIdleCheck, 1000);

    // ── Translation controls ────────────────────────────────────────────
    // Whisper here is the multilingual model, so <|translate|> in place of
    // <|transcribe|> in the decode prefix turns it into a 99-language ->
    // English translator with no second model and no extra pass over the
    // audio. Source "auto" costs one additional decoder step to read the
    // language token the model predicts first.
    var xlateToggle = document.getElementById("translate-enabled");
    var xlateSource = document.getElementById("translate-source");

    function loadTranslationSettings() {
        if (!xlateToggle || !xlateSource) return;
        fetch("/api/languages")
            .then(function (r) { return r.json(); })
            .then(function (d) {
                xlateSource.innerHTML = "";
                (d.languages || []).forEach(function (l) {
                    var o = document.createElement("option");
                    o.value = l.code;
                    o.textContent = l.code === "auto" ? l.name : l.name;
                    xlateSource.appendChild(o);
                });
                xlateSource.value = d.source_language || "auto";
                xlateToggle.checked = !!d.translate_enabled;
            })
            .catch(function () {});
    }

    function saveTranslationSettings() {
        if (!xlateToggle || !xlateSource) return;
        fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                translate_enabled: xlateToggle.checked,
                source_language: xlateSource.value,
            }),
        }).catch(function () {});
    }

    if (xlateToggle) xlateToggle.addEventListener("change", saveTranslationSettings);
    if (xlateSource) xlateSource.addEventListener("change", saveTranslationSettings);

    function flashTranscribed() {
        if (!tsEl) return;
        tsFlashUntil = Date.now() + 1200;
        tsEl.className = "ts-status hit";
        if (tsLabel) tsLabel.textContent = "transcribed";
        if (tsBar) tsBar.style.width = "100%";
    }

    function addTranscriptEntry(data) {
        var entry = document.createElement("div");
        entry.className = "transcript-entry";
        // Mark translated lines with their SOURCE language. English text that
        // silently came from Spanish audio is not something a reader should
        // have to infer, and a wrong detection is only debuggable if shown.
        var tag = "";
        if (data.task === "translate") {
            var lang = (data.language || "??").toUpperCase();
            var conf = data.language_confidence;
            tag = '<span class="xlate-tag" title="Translated to English from '
                + escapeHtml(lang) + (conf ? " (confidence " + conf + ")" : "")
                + '">' + escapeHtml(lang) + '&rarr;EN</span> ';
        }
        entry.innerHTML =
            '<span class="ts">' + escapeHtml(data.timestamp) + '</span> ' +
            '<span class="freq">[' + escapeHtml(data.label) + ']</span> ' +
            tag +
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
        setBadge("decoders", flights.length ? String(flights.length) : "",
                 flights.length ? "live" : "");
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

    // ── Radio activity ──
    pollRadioActivity();
    setInterval(pollRadioActivity, 5000);

    // ── Tabbed workspace ──
    wireViews();
    setInterval(function () {
        var m = document.getElementById("view-model");
        if (m && m.classList.contains("active")) refreshModelView();
    }, 5000);

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
