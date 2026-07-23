// WEFAX panel — broadcast schedule, active reception, decoded chart display, history

(function () {
    "use strict";

    function WefaxPanel(socket) {
        this.socket = socket;
        this.schedule = [];
        this.history = [];
        this.countdownInterval = null;
        this.activeFilter = null;

        this._bindEvents();
        this._fetchSchedule();
        this._fetchLatestImage();
        this._fetchHistory();
    }

    WefaxPanel.prototype._bindEvents = function () {
        var self = this;

        this.socket.on("wefax_broadcast_upcoming", function (data) {
            self._onBroadcastUpcoming(data);
        });

        this.socket.on("wefax_image_ready", function (data) {
            self._onImageReady(data);
        });

        this.socket.on("status", function (data) {
            self._updateRecordingStatus(data);
        });

        // Manual "Receive Now" capture
        var recordBtn = document.getElementById("wefax-record-btn");
        if (recordBtn) {
            recordBtn.addEventListener("click", function () {
                self._triggerManualCapture();
            });
        }

        // Filter buttons
        var filterBtns = document.querySelectorAll(".wefax-filter-btn");
        filterBtns.forEach(function (btn) {
            btn.addEventListener("click", function () {
                var type = btn.dataset.chartType || null;
                self.activeFilter = (self.activeFilter === type) ? null : type;
                filterBtns.forEach(function (b) {
                    b.classList.toggle("active", b.dataset.chartType === self.activeFilter);
                });
                self._fetchHistory();
            });
        });
    };

    WefaxPanel.prototype._triggerManualCapture = function () {
        var btn = document.getElementById("wefax-record-btn");
        var status = document.getElementById("wefax-manual-status");
        var freq = parseFloat(document.getElementById("wefax-manual-freq").value);
        var dur = parseInt(document.getElementById("wefax-manual-dur").value, 10);
        if (btn) { btn.disabled = true; }
        if (status) { status.textContent = "Starting capture…"; }
        fetch("/api/wefax/record", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ frequency_khz: freq, duration_minutes: dur })
        })
            .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
            .then(function (res) {
                if (status) {
                    status.textContent = res.ok
                        ? ("Receiving " + freq + " kHz for " + dur + " min — chart will appear below when decoded.")
                        : ("Error: " + (res.j.error || "could not start"));
                }
            })
            .catch(function () { if (status) { status.textContent = "Request failed."; } })
            .finally(function () { if (btn) { btn.disabled = false; } });
    };

    WefaxPanel.prototype._fetchSchedule = function () {
        var self = this;
        fetch("/api/wefax/schedule")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (Array.isArray(data)) {
                    self.schedule = data;
                    self._renderSchedule();
                    self._startCountdown();
                }
            })
            .catch(function () {});
    };

    WefaxPanel.prototype._fetchLatestImage = function () {
        var self = this;
        fetch("/api/wefax/latest")
            .then(function (r) {
                if (r.ok) return r.json();
                return null;
            })
            .then(function (data) {
                if (data && data.url) {
                    self._displayChart(data);
                }
            })
            .catch(function () {});
    };

    WefaxPanel.prototype._fetchHistory = function () {
        var self = this;
        var url = "/api/wefax/history";
        if (this.activeFilter) {
            url += "?chart_type=" + encodeURIComponent(this.activeFilter);
        }
        fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (Array.isArray(data)) {
                    self.history = data;
                    self._renderHistory();
                }
            })
            .catch(function () {});
    };

    WefaxPanel.prototype._renderSchedule = function () {
        var list = document.getElementById("wefax-schedule-list");
        if (!list) return;

        // Clear existing children safely
        while (list.firstChild) list.removeChild(list.firstChild);
        var upcoming = this.schedule.slice(0, 5);

        if (upcoming.length === 0) {
            var noData = document.createElement("div");
            noData.className = "wefax-no-data";
            noData.textContent = "No broadcasts in next 6h";
            list.appendChild(noData);
            return;
        }

        upcoming.forEach(function (b) {
            var item = document.createElement("div");
            item.className = "wefax-schedule-item";

            var infoRow = document.createElement("div");
            infoRow.className = "wefax-schedule-info";

            var stationBadge = document.createElement("span");
            stationBadge.className = "wefax-station-badge";
            stationBadge.textContent = b.station;
            infoRow.appendChild(stationBadge);

            var freqBadge = document.createElement("span");
            freqBadge.className = "wefax-freq-badge";
            freqBadge.textContent = b.frequency_khz.toFixed(0) + " kHz";
            infoRow.appendChild(freqBadge);

            var detailRow = document.createElement("div");
            detailRow.className = "wefax-schedule-detail";

            var desc = document.createElement("span");
            desc.className = "wefax-chart-desc" + (b.priority ? " wefax-priority" : "");
            desc.textContent = b.description;
            detailRow.appendChild(desc);

            var startDate = new Date(b.start_utc);
            var timeStr = startDate.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
            var timeSpan = document.createElement("span");
            timeSpan.className = "wefax-schedule-time";
            timeSpan.textContent = timeStr;
            detailRow.appendChild(timeSpan);

            item.appendChild(infoRow);
            item.appendChild(detailRow);
            list.appendChild(item);
        });
    };

    WefaxPanel.prototype._startCountdown = function () {
        var self = this;
        if (this.countdownInterval) clearInterval(this.countdownInterval);

        this.countdownInterval = setInterval(function () {
            self._updateCountdown();
        }, 1000);
        this._updateCountdown();
    };

    WefaxPanel.prototype._updateCountdown = function () {
        var el = document.getElementById("wefax-countdown");
        if (!el) return;

        if (this.schedule.length === 0) {
            el.textContent = "--:--:--";
            return;
        }

        var now = new Date();
        var nextStart = new Date(this.schedule[0].start_utc);
        var diff = Math.max(0, Math.floor((nextStart - now) / 1000));

        if (diff <= 0) {
            el.textContent = "NOW";
            el.classList.add("wefax-countdown-now");
            return;
        }

        el.classList.remove("wefax-countdown-now");
        var h = Math.floor(diff / 3600);
        var m = Math.floor((diff % 3600) / 60);
        var s = diff % 60;
        el.textContent =
            (h > 0 ? h + "h " : "") +
            (m < 10 ? "0" : "") + m + ":" +
            (s < 10 ? "0" : "") + s;
    };

    WefaxPanel.prototype._updateRecordingStatus = function (status) {
        var indicator = document.getElementById("wefax-recording-indicator");
        if (!indicator) return;

        if (status.wefax_recording) {
            indicator.classList.remove("hidden");
            indicator.classList.add("recording");
        } else {
            indicator.classList.add("hidden");
            indicator.classList.remove("recording");
        }
    };

    WefaxPanel.prototype._onBroadcastUpcoming = function (data) {
        this._fetchSchedule();
        var label = document.getElementById("wefax-next-label");
        if (label) {
            label.textContent = data.station + " " + data.description + " in " + data.minutes_until + " min";
        }
    };

    WefaxPanel.prototype._onImageReady = function (data) {
        this._displayChart(data);
        this._fetchHistory();
        this._fetchSchedule();
    };

    WefaxPanel.prototype._displayChart = function (data) {
        var container = document.getElementById("wefax-latest-chart");
        if (!container) return;

        while (container.firstChild) container.removeChild(container.firstChild);

        var meta = document.createElement("div");
        meta.className = "wefax-chart-meta";

        var stBadge = document.createElement("span");
        stBadge.className = "wefax-station-badge";
        stBadge.textContent = data.station || "";
        meta.appendChild(stBadge);

        var ctSpan = document.createElement("span");
        ctSpan.className = "wefax-chart-type";
        ctSpan.textContent = (data.chart_type || "").replace(/_/g, " ");
        meta.appendChild(ctSpan);

        var timeSpan = document.createElement("span");
        timeSpan.className = "wefax-chart-time";
        timeSpan.textContent = data.decoded_at || "";
        meta.appendChild(timeSpan);

        container.appendChild(meta);

        var img = document.createElement("img");
        img.src = data.url;
        img.alt = "WEFAX chart";
        img.className = "wefax-decoded-img";
        img.addEventListener("click", function () {
            img.classList.toggle("expanded");
        });
        container.appendChild(img);
    };

    WefaxPanel.prototype._renderHistory = function () {
        var historyEl = document.getElementById("wefax-chart-history");
        if (!historyEl) return;

        while (historyEl.firstChild) historyEl.removeChild(historyEl.firstChild);

        if (this.history.length === 0) {
            var noData = document.createElement("div");
            noData.className = "wefax-no-data";
            noData.textContent = "No decoded charts yet";
            historyEl.appendChild(noData);
            return;
        }

        var self = this;
        this.history.forEach(function (item) {
            var thumb = document.createElement("div");
            thumb.className = "wefax-thumb";

            var img = document.createElement("img");
            img.src = item.url;
            img.alt = (item.chart_type || "").replace(/_/g, " ");
            thumb.appendChild(img);

            var label = document.createElement("span");
            label.className = "wefax-thumb-label";
            label.textContent = item.station || "";
            thumb.appendChild(label);

            thumb.addEventListener("click", function () {
                self._displayChart(item);
            });

            historyEl.appendChild(thumb);
        });
    };

    WefaxPanel.prototype.show = function () {
        var panel = document.getElementById("wefax-panel");
        if (panel) panel.classList.remove("hidden");
    };

    WefaxPanel.prototype.hide = function () {
        var panel = document.getElementById("wefax-panel");
        if (panel) panel.classList.add("hidden");
    };

    // Export
    window.WefaxPanel = WefaxPanel;
})();
