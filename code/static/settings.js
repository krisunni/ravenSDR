// Settings panel — NPU keyword watchlist, analysis thresholds, training corpus.
// Reads/writes /api/config (settings block) and /api/training/stats.

(function () {
    "use strict";

    var THRESHOLDS = [
        { key: "sei_match_threshold", slider: "thr-sei", val: "thr-sei-val", fmt: function (v) { return Number(v).toFixed(2); } },
        { key: "classifier_confidence", slider: "thr-classifier", val: "thr-classifier-val", fmt: function (v) { return Number(v).toFixed(2); } },
        { key: "segmenter_threshold_db", slider: "thr-segmenter", val: "thr-segmenter-val", fmt: function (v) { return Number(v) + " dB"; } },
        { key: "silence_threshold", slider: "thr-silence", val: "thr-silence-val", fmt: function (v) { return String(Math.round(v)); } },
    ];

    function SettingsPanel(socket) {
        this.socket = socket;
        this.settings = null;
        this._bindUi();
        this._fetch();
        this._fetchTraining();
    }

    SettingsPanel.prototype._bindUi = function () {
        var self = this;
        var toggle = document.getElementById("settings-toggle");
        var close = document.getElementById("settings-close");
        if (toggle) toggle.addEventListener("click", function () { self.toggle(); });
        if (close) close.addEventListener("click", function () { self.hide(); });

        var kwAdd = document.getElementById("kw-add");
        if (kwAdd) kwAdd.addEventListener("click", function () { self._addKeyword(); });
        var kwTerm = document.getElementById("kw-term");
        if (kwTerm) kwTerm.addEventListener("keydown", function (e) {
            if (e.key === "Enter") self._addKeyword();
        });

        var kwEnabled = document.getElementById("kw-enabled");
        if (kwEnabled) kwEnabled.addEventListener("change", function () {
            self._patch({ keywords_enabled: kwEnabled.checked });
        });

        THRESHOLDS.forEach(function (t) {
            var el = document.getElementById(t.slider);
            if (!el) return;
            // live label while dragging
            el.addEventListener("input", function () {
                var lab = document.getElementById(t.val);
                if (lab) lab.textContent = t.fmt(el.value);
            });
            // persist on release
            el.addEventListener("change", function () {
                var patch = {};
                patch[t.key] = t.key === "segmenter_threshold_db" || t.key === "silence_threshold"
                    ? parseInt(el.value, 10) : parseFloat(el.value);
                self._patch(patch);
            });
        });
    };

    SettingsPanel.prototype._fetch = function () {
        var self = this;
        fetch("/api/config")
            .then(function (r) { return r.json(); })
            .then(function (s) { self.settings = s; self._render(); })
            .catch(function () {});
    };

    SettingsPanel.prototype._fetchTraining = function () {
        fetch("/api/training/stats")
            .then(function (r) { return r.json(); })
            .then(function (t) { renderTraining(t); })
            .catch(function () {});
    };

    // POST a partial patch; server returns the merged settings.
    SettingsPanel.prototype._patch = function (patch) {
        var self = this;
        fetch("/api/config", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(patch),
        })
            .then(function (r) { return r.json(); })
            .then(function (s) { if (!s.error) { self.settings = s; self._render(); } })
            .catch(function () {});
    };

    SettingsPanel.prototype._addKeyword = function () {
        var term = (document.getElementById("kw-term").value || "").trim();
        if (!term) return;
        var severity = document.getElementById("kw-severity").value;
        var list = (this.settings && this.settings.keywords ? this.settings.keywords.slice() : []);
        list.push({ term: term, severity: severity, enabled: true });
        document.getElementById("kw-term").value = "";
        this._patch({ keywords: list });
    };

    SettingsPanel.prototype._removeKeyword = function (idx) {
        var list = (this.settings && this.settings.keywords ? this.settings.keywords.slice() : []);
        list.splice(idx, 1);
        this._patch({ keywords: list });
    };

    SettingsPanel.prototype._toggleKeyword = function (idx, enabled) {
        var list = (this.settings && this.settings.keywords ? this.settings.keywords.slice() : []);
        if (list[idx]) list[idx].enabled = enabled;
        this._patch({ keywords: list });
    };

    SettingsPanel.prototype._render = function () {
        if (!this.settings) return;
        var self = this;

        var kwEnabled = document.getElementById("kw-enabled");
        if (kwEnabled) kwEnabled.checked = this.settings.keywords_enabled !== false;

        var listEl = document.getElementById("kw-list");
        if (listEl) {
            while (listEl.firstChild) listEl.removeChild(listEl.firstChild);
            var kws = this.settings.keywords || [];
            if (kws.length === 0) {
                var empty = document.createElement("div");
                empty.className = "settings-hint";
                empty.textContent = "No keywords yet.";
                listEl.appendChild(empty);
            }
            kws.forEach(function (kw, idx) {
                var row = document.createElement("div");
                row.className = "kw-row kw-sev-" + (kw.severity || "info");

                var chk = document.createElement("input");
                chk.type = "checkbox";
                chk.checked = kw.enabled !== false;
                chk.addEventListener("change", function () { self._toggleKeyword(idx, chk.checked); });
                row.appendChild(chk);

                var term = document.createElement("span");
                term.className = "kw-term";
                term.textContent = kw.term;
                row.appendChild(term);

                var sev = document.createElement("span");
                sev.className = "kw-badge kw-badge-" + (kw.severity || "info");
                sev.textContent = kw.severity || "info";
                row.appendChild(sev);

                var del = document.createElement("button");
                del.className = "btn btn-sm kw-del";
                del.textContent = "×";
                del.title = "Remove";
                del.addEventListener("click", function () { self._removeKeyword(idx); });
                row.appendChild(del);

                listEl.appendChild(row);
            });
        }

        THRESHOLDS.forEach(function (t) {
            var el = document.getElementById(t.slider);
            var lab = document.getElementById(t.val);
            if (el && self.settings[t.key] != null) el.value = self.settings[t.key];
            if (lab && self.settings[t.key] != null) lab.textContent = t.fmt(self.settings[t.key]);
        });
    };

    function renderTraining(t) {
        var note = document.getElementById("training-note");
        if (note) note.textContent = t.note || "";
        var el = document.getElementById("training-stats");
        if (!el) return;
        while (el.firstChild) el.removeChild(el.firstChild);

        var summary = document.createElement("div");
        summary.className = "training-summary";
        summary.textContent = t.enrolled_emitters + " enrolled emitters · "
            + t.collected_samples + " collected IQ samples · "
            + (t.collected_bytes / 1048576).toFixed(1) + " MB";
        el.appendChild(summary);

        (t.per_emitter || []).forEach(function (e) {
            var row = document.createElement("div");
            row.className = "training-row";
            row.textContent = e.label + ": " + e.samples + " samples";
            el.appendChild(row);
        });
    }

    SettingsPanel.prototype.toggle = function () {
        var panel = document.getElementById("settings-panel");
        if (!panel) return;
        if (panel.classList.contains("hidden")) { this.show(); } else { this.hide(); }
    };

    SettingsPanel.prototype.show = function () {
        var panel = document.getElementById("settings-panel");
        if (panel) panel.classList.remove("hidden");
        this._fetch();
        this._fetchTraining();
    };

    SettingsPanel.prototype.hide = function () {
        var panel = document.getElementById("settings-panel");
        if (panel) panel.classList.add("hidden");
    };

    window.SettingsPanel = SettingsPanel;
})();
