// ravenSDR — Leaflet aircraft map

(function () {
    "use strict";

    var SEATAC = [47.4502, -122.3088];
    var map = null;
    var markers = {};
    var flightCount = document.getElementById("flight-count");

    // SVG aircraft icon
    var AIRCRAFT_SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="20" height="20">' +
        '<path d="M12 2L8 9H3l2 3-2 3h5l4 7 4-7h5l-2-3 2-3h-5L12 2z"/>' +
        '</svg>';

    function aircraftIcon(highlighted) {
        return L.divIcon({
            className: "aircraft-icon" + (highlighted ? " highlighted" : ""),
            html: AIRCRAFT_SVG,
            iconSize: [20, 20],
            iconAnchor: [10, 10],
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

                if (markers[id]) {
                    markers[id].setLatLng([f.lat, f.lon]);
                    if (typeof markers[id].setRotationAngle === "function") {
                        markers[id].setRotationAngle(f.track || 0);
                    }
                } else {
                    var opts = {
                        icon: aircraftIcon(false),
                    };
                    if (f.track !== undefined) {
                        opts.rotationAngle = f.track;
                    }
                    markers[id] = L.marker([f.lat, f.lon], opts).addTo(map);
                }

                var label = (f.flight || f.hex || "???").trim();
                var alt = f.altitude ? " " + f.altitude + "ft" : "";
                var spd = f.speed ? " " + f.speed + "kt" : "";
                markers[id].bindTooltip(label + alt + spd, { permanent: false });
            });

            // Remove stale markers
            Object.keys(markers).forEach(function (id) {
                if (!seen[id]) {
                    map.removeLayer(markers[id]);
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
                    markers[id].setIcon(aircraftIcon(true));
                    // Pan to matched aircraft
                    map.panTo(markers[id].getLatLng());
                    // Reset after 8 seconds
                    setTimeout(function () {
                        if (markers[id]) {
                            markers[id].setIcon(aircraftIcon(false));
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
