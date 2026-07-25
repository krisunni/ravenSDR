// Pager panel — scrolling POCSAG/FLEX message feed.

(function () {
    "use strict";

    var MAX_FEED = 120;

    function PagerPanel(socket) {
        this.socket = socket;
        this.messages = [];   // most-recent first
        this.addresses = 0;
        var self = this;

        this.socket.on("pager_message", function (m) { self._prepend(m); });
        this.socket.on("pager_update", function (list) {
            self.addresses = (list || []).length;
            self._renderCount();
        });
    }

    PagerPanel.prototype._prepend = function (m) {
        this.messages.unshift(m);
        if (this.messages.length > MAX_FEED) this.messages = this.messages.slice(0, MAX_FEED);
        var status = document.getElementById("pager-status");
        if (status) status.textContent = "Latest: " + (m.protocol || "") + " " + (m.address || "");
        this._renderFeed();
    };

    PagerPanel.prototype._renderCount = function () {
        var el = document.getElementById("pager-count");
        if (el) el.textContent = this.addresses + " addresses";
    };

    PagerPanel.prototype._renderFeed = function () {
        var feed = document.getElementById("pager-feed");
        if (!feed) return;
        while (feed.firstChild) feed.removeChild(feed.firstChild);

        if (this.messages.length === 0) {
            var none = document.createElement("div");
            none.className = "sei-no-data";
            none.textContent = "No pages decoded yet";
            feed.appendChild(none);
            return;
        }

        this.messages.forEach(function (m) {
            var item = document.createElement("div");
            item.className = "sei-event-item";

            var ts = document.createElement("span");
            ts.className = "sei-event-ts";
            ts.textContent = m.seen ? new Date(m.seen * 1000).toLocaleTimeString() : "";
            item.appendChild(ts);

            var badge = document.createElement("span");
            badge.className = "sei-badge sei-badge-known";
            badge.textContent = m.protocol || "POCSAG";
            item.appendChild(badge);

            var addr = document.createElement("span");
            addr.className = "sei-event-id";
            addr.textContent = m.address || "—";
            item.appendChild(addr);

            if (m.text) {
                var txt = document.createElement("div");
                txt.className = "acars-text";
                txt.textContent = m.text;
                item.appendChild(txt);
            }

            feed.appendChild(item);
        });
    };

    PagerPanel.prototype.show = function () {
        var p = document.getElementById("pager-panel");
        if (p) p.classList.remove("hidden");
        var self = this;
        fetch("/api/pager/pages")
            .then(function (r) { return r.json(); })
            .then(function (list) { self.addresses = (list || []).length; self._renderCount(); })
            .catch(function () {});
    };

    PagerPanel.prototype.hide = function () {
        var p = document.getElementById("pager-panel");
        if (p) p.classList.add("hidden");
    };

    window.PagerPanel = PagerPanel;
})();
