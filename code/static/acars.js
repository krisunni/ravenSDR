// ACARS panel — scrolling aircraft-message feed with ADS-B correlation badges.

(function () {
    "use strict";

    var MAX_FEED = 100;

    function AcarsPanel(socket) {
        this.socket = socket;
        this.messages = [];   // most-recent first
        this.aircraft = 0;
        var self = this;

        // Live per-message events (carry ADS-B correlation when matched)
        this.socket.on("acars_message", function (msg) {
            self._prepend(msg);
        });
        // Periodic full table → keep the aircraft count fresh
        this.socket.on("acars_update", function (list) {
            self.aircraft = (list || []).length;
            self._renderCount();
        });
    }

    AcarsPanel.prototype._prepend = function (msg) {
        this.messages.unshift(msg);
        if (this.messages.length > MAX_FEED) this.messages = this.messages.slice(0, MAX_FEED);
        var status = document.getElementById("acars-status");
        if (status) {
            status.textContent = "Latest: " + (msg.flight || msg.tail || "?")
                + (msg.label ? " [" + msg.label + "]" : "");
        }
        this._renderFeed();
    };

    AcarsPanel.prototype._renderCount = function () {
        var el = document.getElementById("acars-count");
        if (el) el.textContent = this.aircraft + " aircraft";
    };

    AcarsPanel.prototype._renderFeed = function () {
        var feed = document.getElementById("acars-feed");
        if (!feed) return;
        while (feed.firstChild) feed.removeChild(feed.firstChild);

        if (this.messages.length === 0) {
            var none = document.createElement("div");
            none.className = "sei-no-data";
            none.textContent = "No ACARS messages yet";
            feed.appendChild(none);
            return;
        }

        this.messages.forEach(function (m) {
            var item = document.createElement("div");
            item.className = "sei-event-item";

            var ts = document.createElement("span");
            ts.className = "sei-event-ts";
            ts.textContent = m.timestamp
                ? new Date(m.timestamp * 1000).toLocaleTimeString()
                : (m.seen ? new Date(m.seen * 1000).toLocaleTimeString() : "");
            item.appendChild(ts);

            var id = document.createElement("span");
            id.className = "sei-event-id";
            id.textContent = (m.flight || "—") + (m.tail ? " / " + m.tail : "");
            item.appendChild(id);

            if (m.label) {
                var label = document.createElement("span");
                label.className = "sei-event-label";
                label.textContent = m.label;
                item.appendChild(label);
            }

            if (m.adsb_hex) {
                var badge = document.createElement("span");
                badge.className = "sei-badge sei-badge-known";
                badge.textContent = "ADS-B ✈ " + (m.adsb_flight || m.adsb_hex);
                badge.title = "Matched a tracked ADS-B flight";
                item.appendChild(badge);
            }

            if (m.text) {
                var txt = document.createElement("div");
                txt.className = "acars-text";
                txt.textContent = m.text;
                item.appendChild(txt);
            }

            feed.appendChild(item);
        });
    };

    AcarsPanel.prototype.show = function () {
        var p = document.getElementById("acars-panel");
        if (p) p.classList.remove("hidden");
        var self = this;
        fetch("/api/acars/messages")
            .then(function (r) { return r.json(); })
            .then(function (list) {
                self.aircraft = (list || []).length;
                self._renderCount();
            })
            .catch(function () {});
    };

    AcarsPanel.prototype.hide = function () {
        var p = document.getElementById("acars-panel");
        if (p) p.classList.add("hidden");
    };

    window.AcarsPanel = AcarsPanel;
})();
