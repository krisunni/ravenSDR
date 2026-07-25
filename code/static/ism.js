// ISM sensors panel — rtl_433 device table (weather stations, TPMS, meters, ...)

(function () {
    "use strict";

    function IsmPanel(socket) {
        this.socket = socket;
        this.devices = [];
        var self = this;
        // Full table snapshot (with TTL expiry) every few seconds
        this.socket.on("ism_update", function (list) {
            self.devices = list || [];
            self._render();
        });
        // Individual device event — flash the status line
        this.socket.on("ism_device", function (dev) {
            var status = document.getElementById("ism-status");
            if (status) status.textContent = "Latest: " + dev.model + " " + (dev.id || "");
        });
    }

    function fmtReadings(d) {
        var parts = [];
        if (d.temperature_C != null) parts.push(d.temperature_C + "°C");
        if (d.humidity != null) parts.push(d.humidity + "%RH");
        if (d.wind_avg_km_h != null) parts.push(d.wind_avg_km_h + "km/h");
        if (d.rain_mm != null) parts.push(d.rain_mm + "mm");
        if (d.pressure_hPa != null) parts.push(d.pressure_hPa + "hPa");
        if (d.pressure_kPa != null) parts.push(d.pressure_kPa + "kPa");
        if (d.moisture != null) parts.push("moist " + d.moisture);
        if (d.battery_ok != null) parts.push("batt " + (d.battery_ok ? "ok" : "low"));
        return parts.join(" · ") || "—";
    }

    IsmPanel.prototype._render = function () {
        var countEl = document.getElementById("ism-count");
        if (countEl) countEl.textContent = this.devices.length + " devices";

        var tbody = document.getElementById("ism-tbody");
        if (!tbody) return;
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);

        if (this.devices.length === 0) {
            var row = document.createElement("tr");
            var td = document.createElement("td");
            td.colSpan = 5;
            td.className = "sei-no-data";
            td.textContent = "No ISM devices heard yet";
            row.appendChild(td);
            tbody.appendChild(row);
            return;
        }

        this.devices.forEach(function (d) {
            var row = document.createElement("tr");
            [
                d.model || "?",
                String(d.id != null ? d.id : ""),
                fmtReadings(d),
                d.rssi != null ? d.rssi + " dB" : "—",
                d.seen ? new Date(d.seen * 1000).toLocaleTimeString() : "—",
            ].forEach(function (text) {
                var cell = document.createElement("td");
                cell.textContent = text;
                row.appendChild(cell);
            });
            tbody.appendChild(row);
        });
    };

    IsmPanel.prototype.show = function () {
        var p = document.getElementById("ism-panel");
        if (p) p.classList.remove("hidden");
        var self = this;
        fetch("/api/ism/devices")
            .then(function (r) { return r.json(); })
            .then(function (list) { self.devices = list || []; self._render(); })
            .catch(function () {});
    };

    IsmPanel.prototype.hide = function () {
        var p = document.getElementById("ism-panel");
        if (p) p.classList.add("hidden");
    };

    window.IsmPanel = IsmPanel;
})();
