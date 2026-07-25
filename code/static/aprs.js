// APRS station panel — packet positions, weather and telemetry.

(function () {
    "use strict";

    function AprsPanel(socket) {
        this.socket = socket;
        this.stations = [];
        this._expanded = {};   // callsign -> raw packet visible, survives re-render
        var self = this;

        // Full table snapshot (with TTL expiry) every few seconds
        this.socket.on("aprs_update", function (list) {
            self.stations = list || [];
            self._render();
        });

        // Individual packet — flash the status line
        this.socket.on("aprs_packet", function (pkt) {
            var status = document.getElementById("aprs-status");
            if (status && pkt) {
                status.textContent = "Latest: " + (pkt.source || "?") +
                    " (" + (pkt.type || "packet") + ")";
            }
        });
    }

    function fmtPosition(s) {
        if (s.lat == null || s.lon == null) return "—";
        return s.lat.toFixed(4) + ", " + s.lon.toFixed(4);
    }

    function fmtInfo(s) {
        var parts = [];
        if (s.weather) {
            var w = s.weather;
            if (w.temperature_F != null) parts.push(w.temperature_F + "°F");
            if (w.humidity_pct != null) parts.push(w.humidity_pct + "%RH");
            if (w.pressure_hPa != null) parts.push(w.pressure_hPa + "hPa");
            if (w.gust_mph != null) parts.push("gust " + w.gust_mph + "mph");
        }
        if (s.status) parts.push(s.status);
        if (s.message) parts.push("→" + (s.addressee || "") + ": " + s.message);
        if (s.comment) parts.push(s.comment);
        if (!parts.length && s.path && s.path.length) parts.push("via " + s.path.join(","));
        return parts.join(" · ") || "—";
    }

    AprsPanel.prototype._render = function () {
        var countEl = document.getElementById("aprs-count");
        if (countEl) countEl.textContent = this.stations.length + " stations";

        var tbody = document.getElementById("aprs-tbody");
        if (!tbody) return;
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

        if (this.stations.length === 0) {
            var row = document.createElement("tr");
            var td = document.createElement("td");
            td.colSpan = 5;
            td.className = "sei-no-data";
            td.textContent = "No APRS stations heard yet";
            row.appendChild(td);
            tbody.appendChild(row);
            return;
        }

        var self = this;
        this.stations.forEach(function (s) {
            var row = document.createElement("tr");
            row.className = "ism-row";
            [
                s.source || "?",
                s.type || "—",
                fmtPosition(s),
                fmtInfo(s),
                s.seen ? new Date(s.seen * 1000).toLocaleTimeString() : "—",
            ].forEach(function (text) {
                var cell = document.createElement("td");
                cell.textContent = text;
                row.appendChild(cell);
            });
            tbody.appendChild(row);

            // Raw TNC2 packet on click — nothing decoded is hidden from view.
            var key = s.source || "?";
            var detail = document.createElement("tr");
            detail.className = "ism-raw-row";
            if (!self._expanded[key]) detail.classList.add("hidden");
            var cell = document.createElement("td");
            cell.colSpan = 5;
            var pre = document.createElement("pre");
            pre.className = "ism-raw";
            pre.textContent = s.raw || JSON.stringify(s, null, 2);
            cell.appendChild(pre);
            detail.appendChild(cell);
            tbody.appendChild(detail);

            row.title = "Click to show the raw TNC2 packet";
            row.addEventListener("click", function () {
                self._expanded[key] = !self._expanded[key];
                detail.classList.toggle("hidden", !self._expanded[key]);
            });
        });
    };

    AprsPanel.prototype.show = function () {
        var p = document.getElementById("aprs-panel");
        if (p) p.classList.remove("hidden");
        var self = this;
        fetch("/api/aprs/stations")
            .then(function (r) { return r.json(); })
            .then(function (list) { self.stations = list || []; self._render(); })
            .catch(function () { /* panel still renders empty */ });
    };

    AprsPanel.prototype.hide = function () {
        var p = document.getElementById("aprs-panel");
        if (p) p.classList.add("hidden");
    };

    window.AprsPanel = AprsPanel;
})();
