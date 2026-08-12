// ravenSDR — Leaflet aircraft map

(function () {
    "use strict";

    // Leaflet renders string tooltip/popup content as HTML (_updateContent does
    // `innerHTML = e`), and everything plotted here arrives off-air from a
    // stranger with a transmitter. AIS ship names and destinations are ITU-R
    // M.1371 6-bit ASCII, a charset that includes < and > — and a 20-character
    // shipname field is enough for <svg onload=...>. Permanent tooltips render
    // with no click at all, so a hostile vessel appearing on the map would be
    // sufficient. Escape everything that came out of a radio.
    function esc(v) {
        if (v === undefined || v === null) return "";
        return String(v)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    var SEATAC = [47.4502, -122.3088];
    var map = null;
    var markers = {};  // hex -> { marker, label }
    var vesselMarkers = {};  // mmsi -> { marker, label }
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

    // Boat SVG pointing up (north), filled with color
    function makeBoatSvg(color) {
        return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 36 36" width="24" height="24">' +
            '<path fill="' + color + '" stroke="#000" stroke-width="1" d="' +
            'M18 4 L10 28 L18 24 L26 28 Z' +
            '"/></svg>';
    }

    function vesselIcon(course) {
        var color = "#00bcd4";
        return L.divIcon({
            className: "vessel-icon",
            html: '<div style="transform: rotate(' + (course || 0) + 'deg); transform-origin: center;">' +
                  makeBoatSvg(color) + '</div>',
            iconSize: [24, 24],
            iconAnchor: [12, 12],
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
                // Also a permanent tooltip, so it renders unprompted. ADS-B
                // callsigns are a narrow charset, but nothing validates what
                // dump1090 hands over, so treat it like any other off-air field.
                var labelText = esc(callsign || f.hex || "???");
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
                        var sq = esc(f.squawk || "");
                        var popup = "<b>" + esc(callsign || f.hex) + "</b><br>" +
                            "ICAO: " + esc(f.hex || "?") + "<br>" +
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

        updateVessels: function (vessels) {
            if (!map) return;

            var seen = {};
            var count = 0;

            vessels.forEach(function (v) {
                if (!v.lat || !v.lon) return;
                var id = v.mmsi;
                if (!id) return;
                seen[id] = true;
                count++;

                var name = esc((v.name || "").trim() || v.mmsi);
                var spd = v.speed !== undefined ? v.speed.toFixed(1) + " kt" : "";
                var labelText = name;
                if (spd) labelText += " " + spd;

                if (vesselMarkers[id]) {
                    vesselMarkers[id].marker.setLatLng([v.lat, v.lon]);
                    vesselMarkers[id].marker.setIcon(vesselIcon(v.course));
                    vesselMarkers[id].label.setLatLng([v.lat, v.lon]);
                    vesselMarkers[id].label.setTooltipContent(labelText);
                } else {
                    var m = L.marker([v.lat, v.lon], {
                        icon: vesselIcon(v.course),
                    }).addTo(map);

                    var lbl = L.marker([v.lat, v.lon], {
                        icon: L.divIcon({ className: "vessel-label-anchor", iconSize: [0, 0] }),
                    }).addTo(map);
                    lbl.bindTooltip(labelText, {
                        permanent: true,
                        direction: "right",
                        offset: [14, 0],
                        className: "vessel-label",
                    });

                    m.on("click", function () {
                        var crs = v.course !== undefined ? Math.round(v.course) + "\u00b0" : "n/a";
                        var hdg = v.heading !== undefined ? Math.round(v.heading) + "\u00b0" : "";
                        var popup = "<b>" + name + "</b><br>" +
                            "MMSI: " + esc(v.mmsi) + "<br>" +
                            (v.ship_type_label ? "Type: " + esc(v.ship_type_label) + "<br>" : "") +
                            (spd ? "Speed: " + spd + "<br>" : "") +
                            "Course: " + crs +
                            (hdg ? "<br>Heading: " + hdg : "") +
                            (v.destination ? "<br>Dest: " + esc(v.destination) + "" : "");
                        m.bindPopup(popup).openPopup();
                    });

                    vesselMarkers[id] = { marker: m, label: lbl };
                }
            });

            // Remove stale vessel markers
            Object.keys(vesselMarkers).forEach(function (id) {
                if (!seen[id]) {
                    map.removeLayer(vesselMarkers[id].marker);
                    map.removeLayer(vesselMarkers[id].label);
                    delete vesselMarkers[id];
                }
            });

            // Update flight count area with vessel count if no aircraft
            if (flightCount && Object.keys(markers).length === 0) {
                flightCount.textContent = count + " vessels";
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
                vesselMarkers = {};
            }
        },
    };
})();
