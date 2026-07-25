// Emitter tracking panel — known emitters table, re-identification feed, new emitter alerts

(function () {
    "use strict";

    function SEIPanel(socket) {
        this.socket = socket;
        this.emitters = [];
        this.recentEvents = [];
        this.status = {};

        this._bindEvents();
        this._fetchEmitters();
        this._fetchStatus();
    }

    SEIPanel.prototype._bindEvents = function () {
        var self = this;

        this.socket.on("emitter_identified", function (data) {
            self._onIdentified(data);
        });

        this.socket.on("new_emitter", function (data) {
            self._onNewEmitter(data);
        });
    };

    SEIPanel.prototype._fetchEmitters = function () {
        var self = this;
        fetch("/api/emitters?limit=50")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                self.emitters = data.emitters || [];
                self.status.emitter_count = data.total || 0;
                self._renderEmitterTable();
                self._renderStats();
            })
            .catch(function () {});
    };

    SEIPanel.prototype._fetchStatus = function () {
        var self = this;
        fetch("/api/sei/status")
            .then(function (r) { return r.json(); })
            .then(function (data) {
                self.status = data;
                self._renderStats();
            })
            .catch(function () {});
    };

    SEIPanel.prototype._onIdentified = function (data) {
        // Prepend to recent events
        this.recentEvents.unshift(data);
        if (this.recentEvents.length > 100) {
            this.recentEvents = this.recentEvents.slice(0, 100);
        }
        this._renderRecentFeed();

        // Update emitter in table
        var found = false;
        for (var i = 0; i < this.emitters.length; i++) {
            if (this.emitters[i].emitter_id === data.emitter_id) {
                this.emitters[i].last_seen = data.timestamp;
                this.emitters[i].observation_count = data.observation_count;
                if (data.label) this.emitters[i].label = data.label;
                found = true;
                break;
            }
        }
        if (!found) this._fetchEmitters();
        else this._renderEmitterTable();
    };

    SEIPanel.prototype._onNewEmitter = function (data) {
        this.recentEvents.unshift(data);
        if (this.recentEvents.length > 100) {
            this.recentEvents = this.recentEvents.slice(0, 100);
        }
        this._renderRecentFeed();
        this._fetchEmitters();

        // Flash the new emitter alert
        var alertEl = document.getElementById("sei-new-alert");
        if (alertEl) {
            alertEl.textContent = "New emitter: " + data.emitter_id;
            alertEl.classList.remove("hidden");
            setTimeout(function () { alertEl.classList.add("hidden"); }, 5000);
        }
    };

    SEIPanel.prototype._renderEmitterTable = function () {
        var tbody = document.getElementById("sei-emitter-tbody");
        if (!tbody) return;

        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

        if (this.emitters.length === 0) {
            var row = document.createElement("tr");
            var td = document.createElement("td");
            td.colSpan = 6;
            td.className = "sei-no-data";
            td.textContent = "No emitters enrolled yet";
            row.appendChild(td);
            tbody.appendChild(row);
            return;
        }

        var self = this;
        this.emitters.forEach(function (em) {
            var row = document.createElement("tr");
            row.className = "sei-emitter-row";

            var idTd = document.createElement("td");
            idTd.className = "sei-emitter-id";
            idTd.textContent = em.emitter_id;
            row.appendChild(idTd);

            var labelTd = document.createElement("td");
            labelTd.className = "sei-emitter-label";
            var labelSpan = document.createElement("span");
            labelSpan.className = "sei-label-text";
            labelSpan.textContent = em.label || "—";
            labelSpan.title = "Click to edit";
            labelSpan.addEventListener("click", function () {
                self._editLabel(em.emitter_id, labelSpan);
            });
            labelTd.appendChild(labelSpan);
            row.appendChild(labelTd);

            var countTd = document.createElement("td");
            countTd.textContent = em.observation_count || 0;
            row.appendChild(countTd);

            var lastTd = document.createElement("td");
            lastTd.className = "sei-last-seen";
            lastTd.textContent = (em.last_seen || "").substring(0, 19).replace("T", " ");
            row.appendChild(lastTd);

            var freqTd = document.createElement("td");
            freqTd.className = "sei-freq-list";
            var freqs = (em.frequency_history || []).map(function (f) {
                return (f / 1e6).toFixed(1);
            });
            freqTd.textContent = freqs.join(", ") || "—";
            row.appendChild(freqTd);

            var actTd = document.createElement("td");
            var delBtn = document.createElement("button");
            delBtn.className = "sei-del-btn";
            delBtn.textContent = "×";
            delBtn.title = "Delete emitter";
            delBtn.addEventListener("click", function () {
                if (!window.confirm("Delete emitter " + em.emitter_id + "?")) return;
                fetch("/api/emitters/" + em.emitter_id, { method: "DELETE" })
                    .then(function () { self._fetchEmitters(); })
                    .catch(function () {});
            });
            actTd.appendChild(delBtn);
            row.appendChild(actTd);

            tbody.appendChild(row);
        });
    };

    SEIPanel.prototype._editLabel = function (emitterId, spanEl) {
        var current = spanEl.textContent === "—" ? "" : spanEl.textContent;
        var input = document.createElement("input");
        input.type = "text";
        input.className = "sei-label-input";
        input.value = current;

        var parent = spanEl.parentNode;
        parent.replaceChild(input, spanEl);
        input.focus();

        var self = this;

        function saveLabel() {
            var newLabel = input.value.trim();
            fetch("/api/emitters/" + emitterId + "/label", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ label: newLabel }),
            })
                .then(function () {
                    spanEl.textContent = newLabel || "—";
                    parent.replaceChild(spanEl, input);
                    self._fetchEmitters();
                })
                .catch(function () {
                    parent.replaceChild(spanEl, input);
                });
        }

        input.addEventListener("blur", saveLabel);
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") saveLabel();
            if (e.key === "Escape") parent.replaceChild(spanEl, input);
        });
    };

    SEIPanel.prototype._renderRecentFeed = function () {
        var feed = document.getElementById("sei-recent-feed");
        if (!feed) return;

        while (feed.firstChild) feed.removeChild(feed.firstChild);

        if (this.recentEvents.length === 0) {
            var noData = document.createElement("div");
            noData.className = "sei-no-data";
            noData.textContent = "No identifications yet";
            feed.appendChild(noData);
            return;
        }

        this.recentEvents.slice(0, 50).forEach(function (evt, idx) {
            var item = document.createElement("div");
            item.className = "sei-event-item";
            if (idx === 0) item.classList.add("sei-event-new");
            if (evt.event === "new_emitter") item.classList.add("sei-event-enrolled");

            var ts = document.createElement("span");
            ts.className = "sei-event-ts";
            ts.textContent = (evt.timestamp || "").substring(11, 19);
            item.appendChild(ts);

            var badge = document.createElement("span");
            badge.className = evt.event === "new_emitter" ? "sei-badge sei-badge-new" : "sei-badge sei-badge-known";
            badge.textContent = evt.event === "new_emitter" ? "NEW" : "ID";
            item.appendChild(badge);

            var id = document.createElement("span");
            id.className = "sei-event-id";
            id.textContent = evt.emitter_id;
            item.appendChild(id);

            if (evt.label) {
                var label = document.createElement("span");
                label.className = "sei-event-label";
                label.textContent = evt.label;
                item.appendChild(label);
            }

            var conf = document.createElement("span");
            conf.className = "sei-event-conf";
            conf.textContent = Math.round((evt.confidence || 0) * 100) + "%";
            item.appendChild(conf);

            feed.appendChild(item);
        });
    };

    SEIPanel.prototype._renderStats = function () {
        var el;

        el = document.getElementById("sei-emitter-count");
        if (el) el.textContent = this.status.emitter_count || 0;

        el = document.getElementById("sei-backend");
        if (el) {
            var b = this.status.backend || "none";
            el.textContent = b === "hailo" ? "Hailo NPU" : b === "cpu" ? "CPU" : "None";
        }
    };

    SEIPanel.prototype.show = function () {
        var panel = document.getElementById("sei-panel");
        if (panel) panel.classList.remove("hidden");
    };

    SEIPanel.prototype.hide = function () {
        var panel = document.getElementById("sei-panel");
        if (panel) panel.classList.add("hidden");
    };

    window.SEIPanel = SEIPanel;
})();
