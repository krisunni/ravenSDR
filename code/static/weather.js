// Weather panel UI component for NOAA weather radio integration

(function () {
    "use strict";

    var MAX_RAW_TRANSCRIPTS = 3;

    function WeatherPanel(socket) {
        this.socket = socket;
        this.container = document.getElementById("weather-panel");
        if (!this.container) return;

        this._rawTranscripts = [];
        this._currentData = null;
        this._alertDismissed = false;

        this._bindEvents();
        this._fetchInitial();
    }

    WeatherPanel.prototype._bindEvents = function () {
        var self = this;

        this.socket.on("weather_update", function (data) {
            self._currentData = data;
            self._addRawTranscript(data.raw_transcript);
            self._render(data);
            // Clear alert banner if no alerts in this update
            if (!data.alerts || data.alerts.length === 0) {
                self._alertDismissed = false;
                self._hideAlert();
            }
        });

        this.socket.on("priority_alert", function (data) {
            self._alertDismissed = false;
            self._showAlert(data);
        });
    };

    WeatherPanel.prototype._fetchInitial = function () {
        var self = this;
        fetch("/api/weather/current")
            .then(function (r) {
                if (!r.ok) return null;
                return r.json();
            })
            .then(function (data) {
                if (data && !data.error) {
                    self._currentData = data;
                    self._addRawTranscript(data.raw_transcript);
                    self._render(data);
                }
            })
            .catch(function () {});
    };

    WeatherPanel.prototype._addRawTranscript = function (text) {
        if (!text) return;
        this._rawTranscripts.unshift({
            text: text,
            time: new Date()
        });
        if (this._rawTranscripts.length > MAX_RAW_TRANSCRIPTS) {
            this._rawTranscripts.pop();
        }
    };

    WeatherPanel.prototype._render = function (data) {
        if (!this.container) return;

        var html = '';

        // Alert banner area
        html += '<div id="weather-alert-banner" class="weather-alert hidden"></div>';

        // Last updated
        html += '<div class="weather-updated">Last updated: ' +
            this._relativeTime(data.parsed_at) +
            ' <span class="weather-confidence">' + data.confidence + '</span></div>';

        // Current conditions card
        html += '<div class="weather-conditions">';
        if (data.temperature) {
            html += '<div class="weather-field">' +
                '<span class="weather-field-label">Temp</span>' +
                '<span class="weather-field-value">' + data.temperature.value + '&deg;' + data.temperature.unit + '</span>' +
                '</div>';
        }
        if (data.wind) {
            var windText = data.wind.speed === 0 ? 'Light & variable' :
                data.wind.direction + ' ' + data.wind.speed + ' ' + data.wind.unit;
            html += '<div class="weather-field">' +
                '<span class="weather-field-label">Wind</span>' +
                '<span class="weather-field-value">' + windText + '</span>' +
                '</div>';
        }
        if (data.visibility) {
            html += '<div class="weather-field">' +
                '<span class="weather-field-label">Vis</span>' +
                '<span class="weather-field-value">' + data.visibility.value + ' ' + data.visibility.unit + '</span>' +
                '</div>';
        }
        html += '</div>';

        // Active alerts
        if (data.alerts && data.alerts.length > 0) {
            html += '<div class="weather-alerts-list">';
            data.alerts.forEach(function (alert) {
                var cls = alert.type === 'warning' ? 'alert-warning' :
                    alert.type === 'watch' ? 'alert-watch' : 'alert-advisory';
                html += '<div class="weather-alert-item ' + cls + '">' +
                    '<span class="alert-type">' + alert.type.toUpperCase() + '</span> ' +
                    '<span class="alert-name">' + escapeHtml(alert.name) + '</span>' +
                    (alert.area ? ' <span class="alert-area">' + escapeHtml(alert.area) + '</span>' : '') +
                    '</div>';
            });
            html += '</div>';
        }

        // Marine forecast
        if (data.marine && data.marine.length > 0) {
            html += '<div class="weather-marine">';
            html += '<div class="weather-section-header" onclick="this.parentNode.classList.toggle(\'expanded\')">Marine Forecast</div>';
            html += '<div class="weather-section-body">';
            data.marine.forEach(function (seg) {
                html += '<div class="marine-segment">' +
                    '<strong>' + escapeHtml(seg.zone) + '</strong> ' +
                    escapeHtml(seg.forecast) +
                    '</div>';
            });
            html += '</div></div>';
        }

        // Raw transcripts
        if (this._rawTranscripts.length > 0) {
            html += '<div class="weather-raw">';
            html += '<div class="weather-section-header" onclick="this.parentNode.classList.toggle(\'expanded\')">Raw Transcripts (' + this._rawTranscripts.length + ')</div>';
            html += '<div class="weather-section-body">';
            this._rawTranscripts.forEach(function (entry) {
                html += '<div class="raw-transcript">' +
                    '<span class="raw-time">' + entry.time.toLocaleTimeString() + '</span> ' +
                    escapeHtml(entry.text) +
                    '</div>';
            });
            html += '</div></div>';
        }

        this.container.querySelector(".weather-content").innerHTML = html;
    };

    WeatherPanel.prototype._showAlert = function (data) {
        if (this._alertDismissed) return;
        var banner = this.container.querySelector("#weather-alert-banner") ||
            document.createElement("div");

        var alertText = '';
        if (data.alerts && data.alerts.length > 0) {
            data.alerts.forEach(function (a) {
                alertText += a.name + (a.area ? ' — ' + a.area : '') + '. ';
            });
        } else {
            alertText = data.raw_snippet || 'Weather alert detected';
        }

        banner.className = "weather-alert";
        banner.innerHTML = '<span class="alert-icon">!</span> ' +
            '<span class="alert-text">' + escapeHtml(alertText) + '</span>' +
            '<button class="alert-dismiss" onclick="this.parentNode.classList.add(\'hidden\')">&times;</button>';
        banner.classList.remove("hidden");
    };

    WeatherPanel.prototype._hideAlert = function () {
        var banner = this.container.querySelector("#weather-alert-banner");
        if (banner) banner.classList.add("hidden");
    };

    WeatherPanel.prototype._relativeTime = function (isoString) {
        if (!isoString) return "never";
        var then = new Date(isoString);
        var now = new Date();
        var diffMs = now - then;
        var diffS = Math.floor(diffMs / 1000);
        if (diffS < 60) return diffS + "s ago";
        var diffM = Math.floor(diffS / 60);
        if (diffM < 60) return diffM + " min ago";
        var diffH = Math.floor(diffM / 60);
        return diffH + "h ago";
    };

    WeatherPanel.prototype.show = function () {
        if (this.container) this.container.classList.remove("hidden");
    };

    WeatherPanel.prototype.hide = function () {
        if (this.container) this.container.classList.add("hidden");
    };

    function escapeHtml(text) {
        var div = document.createElement("div");
        div.textContent = text || "";
        return div.innerHTML;
    }

    // Export
    window.WeatherPanel = WeatherPanel;

})();
