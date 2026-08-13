// Spectrum survey panel — sweep a band, see what is on the air.
//
// The plot is a filled spectrum coloured by amplitude rather than a plain
// line, because the useful question is "where is there energy" and colour
// carries that at a glance across a thousand bins. Same blue -> cyan -> green
// -> yellow -> red ramp as the classifier waterfall, so a level looks the same
// in both places.

(function () {
    "use strict";

    function SurveyPanel(socket) {
        this.socket = socket;
        this.bands = [];
        this.presets = [];
        this.snapshot = null;
        this.spectrum = null;

        this.canvas = document.getElementById("survey-canvas");
        this.ctx = this.canvas ? this.canvas.getContext("2d") : null;

        this._bindUi();
        this._bindEvents();
        this._loadBands();
        this._loadPresets();
        this._refresh();
    }

    // ── colour ramp ──────────────────────────────────────────────────────
    // t is 0..1 within the band's own dynamic range, NOT an absolute dBm: the
    // floor moves with gain, antenna and band, so a fixed scale would make one
    // band all blue and another all red.
    SurveyPanel.prototype._color = function (t) {
        t = Math.max(0, Math.min(1, t));
        var stops = [
            [0.00, 12, 18, 40],
            [0.25, 20, 90, 170],
            [0.50, 30, 180, 150],
            [0.70, 120, 210, 70],
            [0.85, 240, 200, 50],
            [1.00, 250, 70, 50]
        ];
        for (var i = 0; i < stops.length - 1; i++) {
            var a = stops[i], b = stops[i + 1];
            if (t >= a[0] && t <= b[0]) {
                var f = (t - a[0]) / (b[0] - a[0]);
                return "rgb(" + Math.round(a[1] + (b[1] - a[1]) * f) + ","
                    + Math.round(a[2] + (b[2] - a[2]) * f) + ","
                    + Math.round(a[3] + (b[3] - a[3]) * f) + ")";
            }
        }
        return "rgb(250,70,50)";
    };

    SurveyPanel.prototype._bindUi = function () {
        var self = this;
        var start = document.getElementById("survey-start");
        var stop = document.getElementById("survey-stop");
        var band = document.getElementById("survey-band");
        if (start) start.addEventListener("click", function () { self._start(); });
        if (stop) stop.addEventListener("click", function () { self._stop(); });
        var ident = document.getElementById("survey-identify");
        if (ident) ident.addEventListener("click", function () { self._identify(); });
        if (band) band.addEventListener("change", function () { self._showBandNote(); });

        // Hover readout: with a thousand bins on screen the only way to ask
        // "what frequency is that spike" is to point at it.
        if (this.canvas) {
            this.canvas.addEventListener("mousemove", function (e) {
                self._hover(e);
            });
            this.canvas.addEventListener("mouseleave", function () {
                var r = document.getElementById("survey-readout");
                if (r) r.textContent = "";
            });
        }
    };

    SurveyPanel.prototype._bindEvents = function () {
        var self = this;
        this.socket.on("sweep_started", function (d) { self._onSnapshot(d); });
        this.socket.on("sweep_progress", function (d) { self._onSnapshot(d); });
        this.socket.on("sweep_complete", function (d) {
            self._onSnapshot(d);
            if (d && d.spectrum) { self.spectrum = d.spectrum; self._draw(); }
            if (d && d.diff) self._renderDiff(d.diff);
        });
        this.socket.on("sweep_identify_progress", function (d) {
            self._setStatus("identifying " + (d.index + 1) + "/" + d.total
                + "  ·  " + (d.freq_hz / 1e6).toFixed(3) + " MHz", true);
        });
        this.socket.on("sweep_identified", function (d) {
            self.identified = (d && d.peaks) || [];
            self._setStatus("identified " + self.identified.length + " signals", false);
            self._renderPeaks(self.identified);
        });
        // A backend restart loses an in-flight sweep; re-read rather than
        // leaving a stale "running" state on screen forever.
        this.socket.on("connect", function () { self._refresh(); });
    };

    SurveyPanel.prototype._loadBands = function () {
        var self = this;
        fetch("/api/sweep/bands")
            .then(function (r) { return r.json(); })
            .then(function (d) {
                self.bands = d.bands || [];
                var sel = document.getElementById("survey-band");
                if (!sel) return;
                sel.innerHTML = "";
                self.bands.forEach(function (b) {
                    var o = document.createElement("option");
                    o.value = b.id;
                    var span = (b.high - b.low) / 1e6;
                    o.textContent = b.label + "  (" + (b.low / 1e6).toFixed(3)
                        + "-" + (b.high / 1e6).toFixed(3) + " MHz)";
                    o.dataset.note = b.note || "";
                    o.dataset.span = span;
                    sel.appendChild(o);
                });
                var fm = self.bands.filter(function (b) { return b.id === "fm"; });
                if (fm.length) sel.value = "fm";
                self._showBandNote();
            })
            .catch(function () {});
    };

    SurveyPanel.prototype._loadPresets = function () {
        var self = this;
        fetch("/api/presets")
            .then(function (r) { return r.json(); })
            .then(function (d) {
                self.presets = (d && d.presets) ? d.presets : (Array.isArray(d) ? d : []);
            })
            .catch(function () {});
    };

    SurveyPanel.prototype._showBandNote = function () {
        var sel = document.getElementById("survey-band");
        var note = document.getElementById("survey-band-note");
        if (!sel || !note) return;
        var opt = sel.options[sel.selectedIndex];
        if (!opt) return;
        note.textContent = opt.dataset.note || "";
    };

    SurveyPanel.prototype._start = function () {
        var self = this;
        var band = document.getElementById("survey-band");
        var gain = document.getElementById("survey-gain");
        var integ = document.getElementById("survey-integration");
        this._setStatus("starting…", true);
        fetch("/api/sweep/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                band: band ? band.value : "fm",
                gain: gain ? Number(gain.value) : 40,
                integration: integ ? Number(integ.value) : 4
            })
        })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
                if (!res.ok) { self._setStatus(res.d.error || "failed", false); return; }
                self._onSnapshot(res.d);
            })
            .catch(function () { self._setStatus("request failed", false); });
    };

    SurveyPanel.prototype._identify = function () {
        var self = this;
        this._setStatus("identifying…", true);
        fetch("/api/sweep/identify", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ limit: 12 })
        })
            .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
            .then(function (res) {
                if (!res.ok) self._setStatus(res.d.error || "could not identify", false);
            })
            .catch(function () { self._setStatus("request failed", false); });
    };

    // NEW is the line worth reading, so it comes first and stays coloured.
    SurveyPanel.prototype._renderDiff = function (diff) {
        var el = document.getElementById("survey-diff");
        if (!el || !diff) return;
        var groups = [
            ["new", "NEW", diff.new || []],
            ["gone", "GONE", diff.gone || []],
            ["stronger", "STRONGER", diff.stronger || []],
            ["weaker", "WEAKER", diff.weaker || []]
        ];
        var any = groups.some(function (g) { return g[2].length; });
        if (!any) {
            el.className = "survey-diff";
            el.innerHTML = '<div class="survey-diff-head">No change since the '
                + 'last sweep of this band (' + (diff.unchanged || 0) + ' signals steady)</div>';
            return;
        }
        var html = '<div class="survey-diff-head">Since the last sweep of this band</div>';
        groups.forEach(function (g) {
            g[2].forEach(function (p) {
                var f = p.freq_hz < 3e6 ? (p.freq_hz / 1e3).toFixed(1) + " kHz"
                    : (p.freq_hz / 1e6).toFixed(4) + " MHz";
                var delta = (p.delta_db !== undefined)
                    ? "  (" + (p.delta_db > 0 ? "+" : "") + p.delta_db + " dB)" : "";
                html += '<div class="survey-diff-row"><span class="survey-tag '
                    + g[0] + '">' + g[1] + '</span><span class="survey-mono">'
                    + f + '</span><span class="survey-dim">+'
                    + p.over_floor_db + ' over floor' + delta + '</span></div>';
            });
        });
        el.className = "survey-diff";
        el.innerHTML = html;
    };

    SurveyPanel.prototype._stop = function () {
        var self = this;
        fetch("/api/sweep/stop", { method: "POST" })
            .then(function () { self._refresh(); })
            .catch(function () {});
    };

    SurveyPanel.prototype._refresh = function () {
        var self = this;
        fetch("/api/sweep?full=1")
            .then(function (r) { return r.json(); })
            .then(function (d) {
                self._onSnapshot(d);
                if (d && d.spectrum) { self.spectrum = d.spectrum; self._draw(); }
            })
            .catch(function () {});
    };

    SurveyPanel.prototype._setStatus = function (text, running) {
        var el = document.getElementById("survey-status");
        if (el) {
            el.textContent = text;
            el.className = "survey-status" + (running ? " running" : "");
        }
        var start = document.getElementById("survey-start");
        var stop = document.getElementById("survey-stop");
        if (start) start.disabled = !!running;
        if (stop) stop.disabled = !running;
    };

    SurveyPanel.prototype._onSnapshot = function (d) {
        if (!d) return;
        this.snapshot = d;
        var bar = document.getElementById("survey-progress-bar");
        if (bar) bar.style.width = Math.round((d.progress || 0) * 100) + "%";

        if (d.running) {
            this._setStatus("sweeping " + Math.round((d.progress || 0) * 100) + "%  ·  "
                + (d.bins_collected || 0) + " bins", true);
        } else if (d.last_error) {
            this._setStatus(d.last_error, false);
        } else if (d.finished_at) {
            this._setStatus("done in " + (d.elapsed_s || "?") + "s  ·  floor "
                + d.noise_floor_db + " dB", false);
        } else {
            this._setStatus("idle", false);
        }
        if (d.peaks) this._renderPeaks(d.identified && d.identified.length ? d.identified : d.peaks);
        var ident = document.getElementById("survey-identify");
        if (ident) ident.disabled = !!(d.running || d.identifying) || !(d.peaks && d.peaks.length);
    };

    // ── the plot ─────────────────────────────────────────────────────────
    SurveyPanel.prototype._draw = function () {
        if (!this.ctx || !this.spectrum || !this.spectrum.dbs) return;
        var ctx = this.ctx, w = this.canvas.width, h = this.canvas.height;
        var dbs = this.spectrum.dbs, freqs = this.spectrum.freqs;
        var n = dbs.length;
        if (!n) return;

        var min = Infinity, max = -Infinity;
        for (var i = 0; i < n; i++) {
            if (dbs[i] < min) min = dbs[i];
            if (dbs[i] > max) max = dbs[i];
        }
        if (max - min < 1) max = min + 1;
        this._min = min; this._max = max;

        ctx.fillStyle = "#0b0f16";
        ctx.fillRect(0, 0, w, h);

        // Horizontal gridlines every 10 dB, so the vertical scale is readable
        // rather than decorative.
        ctx.strokeStyle = "rgba(139,148,158,0.15)";
        ctx.lineWidth = 1;
        ctx.font = "10px monospace";
        ctx.fillStyle = "rgba(139,148,158,0.7)";
        var startDb = Math.ceil(min / 10) * 10;
        for (var db = startDb; db <= max; db += 10) {
            var y = h - ((db - min) / (max - min)) * h;
            ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
            ctx.fillText(db + " dB", 4, y - 2);
        }

        // One vertical bar per bin, coloured by height. Bars rather than a line
        // because adjacent bins are independent measurements, not a continuous
        // function — and colour makes a 20 dB spike obvious in peripheral view.
        var barW = Math.max(1, w / n);
        for (var j = 0; j < n; j++) {
            var t = (dbs[j] - min) / (max - min);
            var bh = t * h;
            ctx.fillStyle = this._color(t);
            ctx.fillRect(j * (w / n), h - bh, barW + 0.5, bh);
        }

        this._renderAxis(freqs);
    };

    SurveyPanel.prototype._renderAxis = function (freqs) {
        var axis = document.getElementById("survey-axis");
        if (!axis || !freqs || !freqs.length) return;
        var lo = freqs[0], hi = freqs[freqs.length - 1];
        var parts = [];
        for (var i = 0; i <= 4; i++) {
            var f = lo + (hi - lo) * (i / 4);
            parts.push("<span>" + (f / 1e6).toFixed(f < 10e6 ? 3 : 2) + "</span>");
        }
        axis.innerHTML = parts.join("") + "<span class='survey-axis-unit'>MHz</span>";
    };

    SurveyPanel.prototype._hover = function (e) {
        if (!this.spectrum || !this.spectrum.freqs) return;
        var rect = this.canvas.getBoundingClientRect();
        var frac = (e.clientX - rect.left) / rect.width;
        var n = this.spectrum.freqs.length;
        var idx = Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1))));
        var f = this.spectrum.freqs[idx], db = this.spectrum.dbs[idx];
        var r = document.getElementById("survey-readout");
        if (r) r.textContent = (f / 1e6).toFixed(4) + " MHz   " + db.toFixed(1) + " dB";
    };

    // ── peaks ────────────────────────────────────────────────────────────
    SurveyPanel.prototype._matchPreset = function (hz) {
        // Within half a channel of a preset counts as "this is that station".
        var best = null, bestDelta = Infinity;
        this.presets.forEach(function (p) {
            var f = String(p.freq || "");
            var mult = f.endsWith("M") ? 1e6 : (f.endsWith("k") ? 1e3 : 1);
            var v = parseFloat(f) * mult;
            if (!v) return;
            var d = Math.abs(v - hz);
            if (d < bestDelta) { bestDelta = d; best = p; }
        });
        // 100 kHz for VHF/UHF, but that is far too wide on MW; scale it.
        var tol = hz < 3e6 ? 6e3 : 1e5;
        return bestDelta <= tol ? best : null;
    };

    SurveyPanel.prototype._renderPeaks = function (peaks) {
        var tbody = document.getElementById("survey-peak-rows");
        var count = document.getElementById("survey-peak-count");
        if (!tbody) return;
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
        if (count) count.textContent = peaks.length ? (peaks.length + " above the floor") : "";

        var self = this;
        if (!peaks.length) {
            var tr0 = document.createElement("tr");
            var td0 = document.createElement("td");
            td0.colSpan = 6;
            td0.className = "survey-dim";
            td0.textContent = "Nothing stood clear of the noise floor in this band.";
            tr0.appendChild(td0); tbody.appendChild(tr0);
            return;
        }

        peaks.forEach(function (p) {
            var tr = document.createElement("tr");
            var hz = p.freq_hz;

            var tdF = document.createElement("td");
            tdF.className = "survey-mono";
            tdF.textContent = hz < 3e6 ? (hz / 1e3).toFixed(1) + " kHz"
                : (hz / 1e6).toFixed(4) + " MHz";
            tr.appendChild(tdF);

            var tdL = document.createElement("td");
            tdL.className = "survey-mono";
            tdL.textContent = p.db + " dB";
            tr.appendChild(tdL);

            var tdO = document.createElement("td");
            var bar = document.createElement("span");
            bar.className = "survey-bar";
            bar.style.width = Math.min(100, p.over_floor_db * 4) + "px";
            bar.style.background = self._color(Math.min(1, p.over_floor_db / 25));
            tdO.appendChild(bar);
            var lbl = document.createElement("span");
            lbl.className = "survey-mono survey-dim";
            lbl.textContent = " +" + p.over_floor_db;
            tdO.appendChild(lbl);
            tr.appendChild(tdO);

            var tdM = document.createElement("td");
            if (p.modulation) {
                var mod = document.createElement("span");
                mod.className = "survey-mod";
                mod.textContent = p.modulation;
                mod.title = "NPU confidence " + (p.confidence || "?");
                tdM.appendChild(mod);
            } else {
                tdM.className = "survey-dim";
                tdM.textContent = "—";
            }
            tr.appendChild(tdM);

            var match = self._matchPreset(hz);
            var tdN = document.createElement("td");
            tdN.textContent = match ? match.label : "unknown";
            if (!match) tdN.className = "survey-dim";
            tr.appendChild(tdN);

            var tdA = document.createElement("td");
            if (match) {
                var btn = document.createElement("button");
                btn.className = "btn btn-sm";
                btn.textContent = "Tune";
                btn.addEventListener("click", function () {
                    fetch("/api/tune", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ preset_id: match.id })
                    }).catch(function () {});
                });
                tdA.appendChild(btn);
            }
            tr.appendChild(tdA);
            tbody.appendChild(tr);
        });
    };

    SurveyPanel.prototype.show = function () {
        var p = document.getElementById("survey-panel");
        if (p) p.classList.remove("hidden");
        this._refresh();
    };

    SurveyPanel.prototype.hide = function () {
        var p = document.getElementById("survey-panel");
        if (p) p.classList.add("hidden");
    };

    window.SurveyPanel = SurveyPanel;
})();
