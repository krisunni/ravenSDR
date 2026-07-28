// Signal classification panel — real-time modulation display, spectrogram waterfall, history feed

(function () {
    "use strict";

    function ClassifierPanel(socket) {
        this.socket = socket;
        this.history = [];
        this.status = {};
        this.spectrogramData = [];  // rolling spectrogram rows for waterfall
        this.canvas = null;
        this.ctx = null;
        this.maxRows = 100;  // 10 seconds at 10 fps

        this._bindEvents();
        this._fetchStatus();
    }

    ClassifierPanel.prototype._bindEvents = function () {
        var self = this;

        this.socket.on("signal_classified", function (data) {
            self._onClassification(data);
        });

        this.socket.on("spectrogram_row", function (row) {
            self._renderWaterfall(row);
        });
    };

    ClassifierPanel.prototype._fetchStatus = function () {
        var self = this;
        fetch("/api/classifier/status")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                self.status = data;
                self._renderStatus();
                self._renderWarning();
            })
            .catch(function () {});
    };

    // Say plainly which predictions can be trusted.
    //
    // Three of the six trained classes were only ever observed on ONE frequency,
    // so a model can score highly on them by recognising that band's noise floor
    // and filter shape rather than the modulation. Held-out-frequency testing is
    // the only way to tell them apart, and it cannot run on a single frequency.
    ClassifierPanel.prototype._renderWarning = function () {
        var el = document.getElementById("clf-warning");
        if (!el || !this.status) return;
        var unproven = this.status.unproven_classes || [];
        var validated = this.status.validated_classes || [];
        if (!unproven.length) { el.classList.add("hidden"); return; }

        el.classList.remove("hidden");
        el.innerHTML =
            "<strong>Model limitations.</strong> Trained on " +
            (validated.length + unproven.length) + " classes; only <strong>" +
            validated.length + "</strong> could be validated across more than one " +
            "frequency (" + validated.map(function (c) {
                return "<code>" + c + "</code>"; }).join(" ") + "). " +
            unproven.map(function (c) {
                return "<code>" + c + "</code>"; }).join(" ") +
            " were each seen on a single frequency, so a confident label there may " +
            "be recognising the <em>band</em> rather than the modulation. Anything " +
            "outside these classes will still be forced into one of them.";
    };

    ClassifierPanel.prototype._onClassification = function (data) {
        // Update current signal display
        this._renderCurrentSignal(data);

        // Prepend to history
        this.history.unshift(data);
        if (this.history.length > 100) {
            this.history = this.history.slice(0, 100);
        }
        this._renderHistory();

        // Update accuracy
        this.status.classifications_total = (this.status.classifications_total || 0) + 1;
        this._renderStatus();
    };

    ClassifierPanel.prototype._renderCurrentSignal = function (data) {
        var modEl = document.getElementById("clf-current-mod");
        var confEl = document.getElementById("clf-current-conf");
        var confBar = document.getElementById("clf-conf-bar-fill");
        var freqEl = document.getElementById("clf-current-freq");

        if (!modEl) return;

        modEl.textContent = data.modulation || "--";
        modEl.className = "clf-mod-type clf-mod-" + (data.modulation || "unknown").toLowerCase();
        // An unproven class gets a visible marker and the reason on hover, so a
        // confident-looking label is never mistaken for a trustworthy one.
        if (data.validation === "unproven") {
            modEl.classList.add("clf-unproven");
            modEl.title = data.caveat || "this class was only observed on one frequency";
        } else {
            modEl.title = "";
        }

        var conf = Math.round((data.confidence || 0) * 100);
        confEl.textContent = conf + "%";

        if (confBar) {
            confBar.style.width = conf + "%";
            if (conf >= 85) {
                confBar.className = "clf-conf-bar-fill clf-conf-high";
            } else if (conf >= 70) {
                confBar.className = "clf-conf-bar-fill clf-conf-mid";
            } else {
                confBar.className = "clf-conf-bar-fill clf-conf-low";
            }
        }

        if (freqEl && data.frequency_hz) {
            freqEl.textContent = (data.frequency_hz / 1e6).toFixed(3) + " MHz";
        }

        // Uncertain indicator
        var uncEl = document.getElementById("clf-uncertain");
        if (uncEl) {
            if (data.uncertain) {
                uncEl.classList.remove("hidden");
            } else {
                uncEl.classList.add("hidden");
            }
        }
    };

    ClassifierPanel.prototype._renderHistory = function () {
        var feed = document.getElementById("clf-history-feed");
        if (!feed) return;

        while (feed.firstChild) feed.removeChild(feed.firstChild);

        if (this.history.length === 0) {
            var noData = document.createElement("div");
            noData.className = "clf-no-data";
            noData.textContent = "No classifications yet";
            feed.appendChild(noData);
            return;
        }

        this.history.slice(0, 50).forEach(function (evt, idx) {
            var item = document.createElement("div");
            item.className = "clf-history-item";
            if (idx === 0) item.classList.add("clf-history-new");
            if (evt.uncertain) item.classList.add("clf-uncertain-item");
            if (evt.validation === "unproven") item.classList.add("clf-unproven-item");

            var ts = document.createElement("span");
            ts.className = "clf-history-ts";
            ts.textContent = (evt.timestamp || "").substring(11, 19);
            item.appendChild(ts);

            var mod = document.createElement("span");
            mod.className = "clf-history-mod clf-mod-" + (evt.modulation || "unknown").toLowerCase();
            mod.textContent = evt.modulation || "?";
            item.appendChild(mod);

            var conf = document.createElement("span");
            conf.className = "clf-history-conf";
            conf.textContent = Math.round((evt.confidence || 0) * 100) + "%";
            item.appendChild(conf);

            var freq = document.createElement("span");
            freq.className = "clf-history-freq";
            if (evt.frequency_hz) {
                freq.textContent = (evt.frequency_hz / 1e6).toFixed(3);
            }
            item.appendChild(freq);

            feed.appendChild(item);
        });
    };

    ClassifierPanel.prototype._renderStatus = function () {
        var el;

        el = document.getElementById("clf-backend");
        if (el) {
            var backend = this.status.backend || "none";
            // "onnx" was missing here, so a working trained model on the CPU
            // displayed as "None" while it was classifying thousands of chunks.
            var labels = {
                hailo: "Hailo NPU",
                onnx: "Trained (CPU)",
                cpu: "Heuristic",
                none: "None"
            };
            el.textContent = labels[backend] || backend;
            el.title = backend === "onnx"
                ? "Trained MobileNetV2 running via onnxruntime on the Pi CPU"
                : backend === "cpu"
                ? "No trained model loaded — hand-written rules, WFM/FM/CW/AM/SSB only"
                : "";
        }

        el = document.getElementById("clf-total");
        if (el) el.textContent = this.status.classifications_total || 0;

        el = document.getElementById("clf-accuracy");
        if (el) {
            var acc = this.status.accuracy_vs_presets || 0;
            el.textContent = Math.round(acc * 100) + "%";
            var compared = this.status.compared_count || 0;
            var correct = this.status.correct_count || 0;
            el.title = correct + "/" + compared + " correct vs preset expected_modulation";
        }
    };

    ClassifierPanel.prototype._renderWaterfall = function (spectrogramRow) {
        if (!this.canvas) {
            this.canvas = document.getElementById("clf-waterfall-canvas");
            if (!this.canvas) return;
            this.ctx = this.canvas.getContext("2d");
        }

        // Add new row
        this.spectrogramData.push(spectrogramRow);
        if (this.spectrogramData.length > this.maxRows) {
            this.spectrogramData.shift();
        }

        var canvas = this.canvas;
        var ctx = this.ctx;
        var w = canvas.width;
        var h = canvas.height;
        var rows = this.spectrogramData;

        ctx.clearRect(0, 0, w, h);

        var rowHeight = h / this.maxRows;
        var colWidth = w / (spectrogramRow.length || 1);

        for (var r = 0; r < rows.length; r++) {
            var row = rows[r];
            var y = h - (rows.length - r) * rowHeight;
            for (var c = 0; c < row.length; c++) {
                var val = row[c]; // 0-255
                ctx.fillStyle = this._waterfallColor(val);
                ctx.fillRect(c * colWidth, y, colWidth + 1, rowHeight + 1);
            }
        }
    };

    ClassifierPanel.prototype._waterfallColor = function (val) {
        // Blue -> cyan -> green -> yellow -> red colormap
        var r, g, b;
        if (val < 64) {
            r = 0; g = 0; b = Math.round(val * 4);
        } else if (val < 128) {
            var t = (val - 64) / 64;
            r = 0; g = Math.round(t * 255); b = Math.round(255 * (1 - t));
        } else if (val < 192) {
            var t = (val - 128) / 64;
            r = Math.round(t * 255); g = 255; b = 0;
        } else {
            var t = (val - 192) / 63;
            r = 255; g = Math.round(255 * (1 - t)); b = 0;
        }
        return "rgb(" + r + "," + g + "," + b + ")";
    };

    ClassifierPanel.prototype.show = function () {
        var panel = document.getElementById("classifier-panel");
        if (panel) panel.classList.remove("hidden");
    };

    ClassifierPanel.prototype.hide = function () {
        var panel = document.getElementById("classifier-panel");
        if (panel) panel.classList.add("hidden");
    };

    // Export
    window.ClassifierPanel = ClassifierPanel;
})();
