// ravenSDR — Leaflet aircraft map

(function () {
    "use strict";

    var SEATAC = [47.4502, -122.3088];
    var map = null;
    var markers = {};  // hex -> { marker, label }
    var flightCount = document.getElementById("flight-count");

    // Airplane SVG pointing up (north), filled with color
    function makeAircraftSvg(color) {
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36" width="28" height="28">' +
            '<path fill="' + color + '" stroke="#000" stroke-width="1" d="' +
            'M18 2 L15 14 L4 18 L15 17 L16 30 L18 34 L20 30 L21 17 L32 18 L21 14 Z' +
            '"/></svg>';
    }

    function aircraftIcon(highlighted, track) {
        var color = highlighted ? "#ff3333" : "#00cc66";
        return L.divIcon({
            className: "aircraft-icon",
            html: '<div style="transform: rotate(' + (track || 0) + 'deg); transform-origin: center;">' +
                  makeAircraftSvg(color) + '</div>',
            iconSize: [28, 28],
            iconAnchor: [14, 14],
        });
    }

    window.ravenMap = {
        init: function () {
            if (map) return;
            var container = document.getElementById("adsb-map");
            if (!container) return;

            map = L.map("adsb-map").setView(SEATAC, 10);
            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                attribution: "&copy; OpenStreetMap contributors",
                maxZoom: 18,
            }).addTo(map);
        },

        updateAircraft: function (flights) {
            if (!map) return;

            var seen = {};
            var count = 0;

            flights.forEach(function (f) {
                if (!f.lat || !f.lon) return;
                var id = f.hex || (f.flight || "").trim();
                if (!id) return;
                seen[id] = true;
                count++;

                var callsign = (f.flight || "").trim();
                var alt = f.altitude ? Math.round(f.altitude / 100) * 100 : "";
                var labelText = callsign || f.hex || "???";
                if (alt) labelText += " " + (alt >= 10000 ? "FL" + Math.round(alt / 100) : alt + "ft");

                if (markers[id]) {
                    markers[id].marker.setLatLng([f.lat, f.lon]);
                    markers[id].marker.setIcon(aircraftIcon(false, f.track));
                    markers[id].label.setLatLng([f.lat, f.lon]);
                    markers[id].label.setTooltipContent(labelText);
                } else {
                    var m = L.marker([f.lat, f.lon], {
                        icon: aircraftIcon(false, f.track),
                    }).addTo(map);

                    // Permanent callsign label
                    var lbl = L.marker([f.lat, f.lon], {
                        icon: L.divIcon({ className: "aircraft-label-anchor", iconSize: [0, 0] }),
                    }).addTo(map);
                    lbl.bindTooltip(labelText, {
                        permanent: true,
                        direction: "right",
                        offset: [16, 0],
                        className: "aircraft-label",
                    });

                    // Detail popup on click
                    m.on("click", function () {
                        var spd = f.speed ? f.speed + " kt" : "n/a";
                        var hdg = f.track !== undefined ? Math.round(f.track) + "\u00b0" : "n/a";
                        var vr = f.vert_rate !== undefined ? (f.vert_rate > 0 ? "+" : "") + f.vert_rate + " fpm" : "";
                        var sq = f.squawk || "";
                        var popup = "<b>" + (callsign || f.hex) + "</b><br>" +
                            "ICAO: " + (f.hex || "?") + "<br>" +
                            "Alt: " + (alt || "?") + "<br>" +
                            "Spd: " + spd + "<br>" +
                            "Hdg: " + hdg +
                            (vr ? "<br>VS: " + vr : "") +
                            (sq ? "<br>Squawk: " + sq : "");
                        m.bindPopup(popup).openPopup();
                    });

                    markers[id] = { marker: m, label: lbl };
                }
            });

            // Remove stale markers
            Object.keys(markers).forEach(function (id) {
                if (!seen[id]) {
                    map.removeLayer(markers[id].marker);
                    map.removeLayer(markers[id].label);
                    delete markers[id];
                }
            });

            if (flightCount) {
                flightCount.textContent = count + " aircraft";
            }
        },

        highlightAircraft: function (matches) {
            if (!map) return;
            matches.forEach(function (m) {
                var id = m.hex || (m.flight || "").trim();
                if (markers[id]) {
                    markers[id].marker.setIcon(aircraftIcon(true, m.track));
                    map.panTo(markers[id].marker.getLatLng());
                    setTimeout(function () {
                        if (markers[id]) {
                            markers[id].marker.setIcon(aircraftIcon(false, m.track));
                        }
                    }, 8000);
                }
            });
        },

        show: function () {
            var panel = document.getElementById("adsb-panel");
            if (panel) {
                panel.classList.remove("hidden");
                if (map) {
                    setTimeout(function () { map.invalidateSize(); }, 100);
                }
            }
        },

        hide: function () {
            var panel = document.getElementById("adsb-panel");
            if (panel) panel.classList.add("hidden");
        },

        setFullWidth: function (full) {
            var panel = document.getElementById("adsb-panel");
            if (panel) {
                panel.classList.toggle("map-fullwidth", full);
                if (map) {
                    setTimeout(function () { map.invalidateSize(); }, 100);
                }
            }
        },

        destroy: function () {
            if (map) {
                map.remove();
                map = null;
                markers = {};
            }
        },
    };
})();
