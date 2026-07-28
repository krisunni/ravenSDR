// ravenSDR explainer — animations.
//
// Canvas for anything signal-shaped (cheap to redraw at 60fps), D3 for the
// charts with axes and live data. D3 is vendored locally: the node is meant to
// run air-gapped, so a CDN at runtime would be a dead link in a field kit.

(function () {
    "use strict";

    var C = {
        bg: "#0d1117", grid: "#21262d", dim: "#8b949e", text: "#e6edf3",
        accent: "#58a6ff", green: "#3fb950", yellow: "#d29922",
        red: "#f85149", purple: "#bc8cff"
    };

    function ctxOf(id) {
        var el = document.getElementById(id);
        if (!el) return null;
        var dpr = window.devicePixelRatio || 1;
        var w = el.width, h = el.height;
        el.style.width = "100%";
        el.style.height = (h / w * el.clientWidth || h) + "px";
        el.width = w * dpr; el.height = h * dpr;
        var ctx = el.getContext("2d");
        ctx.scale(dpr, dpr);
        ctx._w = w; ctx._h = h;
        return ctx;
    }

    function wire(sel, cb) {
        var btns = document.querySelectorAll(sel);
        btns.forEach(function (b) {
            b.addEventListener("click", function () {
                btns.forEach(function (o) { o.classList.remove("active"); });
                b.classList.add("active");
                cb(b.dataset);
            });
        });
    }

    // ── 1. the spinning arrow ────────────────────────────────────────────
    (function spinningVector() {
        var ctx = ctxOf("c-vector");
        if (!ctx) return;
        var speed = 1.0, amp = 1.0, t = 0;
        var hist = [];

        wire("[data-vec]", function (d) {
            speed = d.vec === "fast" ? 2.4 : 1.0;
            amp = d.vec === "big" ? 1.0 : 0.66;
            if (d.vec === "big") speed = 1.0;
            hist = [];
        });
        amp = 0.66;

        function draw() {
            var w = ctx._w, h = ctx._h, cx = 120, cy = h / 2, R = 78 * amp;
            ctx.clearRect(0, 0, w, h);

            // circle the arrow sweeps
            ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.arc(cx, cy, 78, 0, Math.PI * 2); ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(cx - 92, cy); ctx.lineTo(cx + 92, cy);
            ctx.moveTo(cx, cy - 92); ctx.lineTo(cx, cy + 92); ctx.stroke();

            var a = t * speed;
            var px = cx + Math.cos(a) * R, py = cy - Math.sin(a) * R;

            // I and Q projections
            ctx.strokeStyle = C.accent; ctx.lineWidth = 2;
            ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(px, cy); ctx.stroke();
            ctx.strokeStyle = C.green;
            ctx.beginPath(); ctx.moveTo(px, cy); ctx.lineTo(px, py); ctx.stroke();

            // the arrow
            ctx.strokeStyle = C.yellow; ctx.lineWidth = 2.5;
            ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(px, py); ctx.stroke();
            ctx.fillStyle = C.yellow;
            ctx.beginPath(); ctx.arc(px, py, 4.5, 0, Math.PI * 2); ctx.fill();

            ctx.font = "11px ui-monospace, monospace";
            ctx.fillStyle = C.accent;
            ctx.fillText("I = " + (Math.cos(a) * amp).toFixed(2), cx - 30, cy + 18);
            ctx.fillStyle = C.green;
            ctx.fillText("Q = " + (Math.sin(a) * amp).toFixed(2), px + 8, (cy + py) / 2);

            // the two numbers traced over time
            hist.push({ i: Math.cos(a) * amp, q: Math.sin(a) * amp });
            if (hist.length > 300) hist.shift();
            var x0 = 250, plotW = w - x0 - 24, midY = cy;
            ctx.strokeStyle = C.grid;
            ctx.beginPath(); ctx.moveTo(x0, midY); ctx.lineTo(x0 + plotW, midY); ctx.stroke();

            [["i", C.accent], ["q", C.green]].forEach(function (pair) {
                ctx.strokeStyle = pair[1]; ctx.lineWidth = 1.8;
                ctx.beginPath();
                hist.forEach(function (p, k) {
                    var x = x0 + (k / 300) * plotW, y = midY - p[pair[0]] * 66;
                    k ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
                });
                ctx.stroke();
            });
            ctx.fillStyle = C.dim; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText("what actually gets written to disk, sample by sample", x0, 24);

            t += 0.035;
            requestAnimationFrame(draw);
        }
        draw();
    })();

    // ── 2. modulation ────────────────────────────────────────────────────
    (function modulation() {
        var ctx = ctxOf("c-mod");
        if (!ctx) return;
        var mode = "OOK", t = 0;
        var bits = [1, 0, 1, 1, 0];
        var notes = {
            OOK: "The carrier is simply switched on and off. Dead simple — but if the " +
                 "signal fades, 'weak on' starts to look like 'off'.",
            FSK: "Two different frequencies. Fading changes how LOUD it is, not which " +
                 "note it is, so this survives a weak signal far better.",
            MSK: "Like FSK, but the two notes are as close as they can be without being " +
                 "confusable, and it slides between them. No abrupt jumps means no " +
                 "splatter into the neighbours' frequencies.",
            AFSK: "The two notes are AUDIO — then that audio is sent over a normal FM " +
                  "radio. The radio thinks it is carrying a voice. Decoding takes two " +
                  "stages: undo the FM, then listen to the beeps."
        };
        var hint = document.getElementById("mod-explain");

        wire("[data-mod]", function (d) { mode = d.mod; hint.textContent = notes[mode]; });
        hint.textContent = notes.OOK;

        function draw() {
            var w = ctx._w, h = ctx._h;
            ctx.clearRect(0, 0, w, h);
            var padL = 24, plotW = w - padL - 20;
            var bitW = plotW / bits.length;

            // bit boundaries + labels
            ctx.font = "12px ui-monospace, monospace";
            bits.forEach(function (b, k) {
                var x = padL + k * bitW;
                ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
                ctx.beginPath(); ctx.moveTo(x, 26); ctx.lineTo(x, h - 12); ctx.stroke();
                ctx.fillStyle = b ? C.green : C.dim;
                ctx.fillText(String(b), x + bitW / 2 - 3, 18);
            });

            var midY = h / 2 + 10, A = 46;
            ctx.strokeStyle = C.yellow; ctx.lineWidth = 1.8;
            ctx.beginPath();
            for (var px = 0; px < plotW; px++) {
                var frac = px / bitW, bi = Math.min(bits.length - 1, Math.floor(frac));
                var bit = bits[bi], y;
                var ph = (px + t) * 0.35;

                if (mode === "OOK") {
                    y = midY - (bit ? Math.sin(ph) * A : 0);
                } else if (mode === "FSK") {
                    y = midY - Math.sin((px + t) * (bit ? 0.55 : 0.22)) * A;
                } else if (mode === "MSK") {
                    // slide the frequency instead of stepping it
                    var nextBit = bits[Math.min(bits.length - 1, bi + 1)];
                    var within = frac - bi;
                    var f = (bit ? 0.42 : 0.26);
                    if (within > 0.8) f += (((nextBit ? 0.42 : 0.26)) - f) * ((within - 0.8) / 0.2);
                    y = midY - Math.sin((px + t) * f) * A;
                } else { // AFSK — audio tones riding an FM carrier
                    var tone = Math.sin((px + t) * (bit ? 0.5 : 0.24));
                    y = midY - Math.sin((px + t) * 0.9 + tone * 2.2) * A * 0.85;
                }
                px ? ctx.lineTo(padL + px, y) : ctx.moveTo(padL + px, y);
            }
            ctx.stroke();

            if (mode === "AFSK") {
                // show the hidden audio tone underneath
                ctx.strokeStyle = C.purple; ctx.lineWidth = 1.2; ctx.globalAlpha = .75;
                ctx.beginPath();
                for (var p2 = 0; p2 < plotW; p2++) {
                    var b2 = bits[Math.min(bits.length - 1, Math.floor(p2 / bitW))];
                    var yy = h - 22 - Math.sin((p2 + t) * (b2 ? 0.5 : 0.24)) * 12;
                    p2 ? ctx.lineTo(padL + p2, yy) : ctx.moveTo(padL + p2, yy);
                }
                ctx.stroke(); ctx.globalAlpha = 1;
                ctx.fillStyle = C.purple; ctx.font = "10px ui-monospace, monospace";
                ctx.fillText("the audio tones hiding inside", padL, h - 40);
            }

            t += 1.3;
            requestAnimationFrame(draw);
        }
        draw();
    })();

    // ── 3. spectrogram builder ───────────────────────────────────────────
    (function spectrogram() {
        var ctx = ctxOf("c-spec");
        if (!ctx) return;
        var rows = [], mode = "two", row = 0, MAXROWS = 120;

        function makeRow(r) {
            var bins = 128, out = new Float32Array(bins);
            for (var i = 0; i < bins; i++) out[i] = Math.random() * 0.16;
            if (mode === "two") {
                [38, 88].forEach(function (c, idx) {
                    for (var k = -4; k <= 4; k++)
                        out[c + k] += (idx ? 0.85 : 1.0) * Math.exp(-k * k / 6);
                });
            } else if (mode === "burst") {
                if ((r % 40) < 9) {
                    for (var k2 = -6; k2 <= 6; k2++)
                        out[64 + k2] += 1.0 * Math.exp(-k2 * k2 / 14);
                }
            }
            return out;
        }

        wire("[data-spec]", function (d) { mode = d.spec; rows = []; row = 0; });
        var rb = document.getElementById("spec-restart");
        if (rb) rb.addEventListener("click", function () { rows = []; row = 0; });

        var acc = 0;
        function draw(ts) {
            var w = ctx._w, h = ctx._h;
            ctx.fillStyle = C.bg; ctx.fillRect(0, 0, w, h);

            acc++;
            if (acc % 2 === 0) {
                rows.push(makeRow(row++));
                if (rows.length > MAXROWS) rows.shift();
            }

            var padL = 300, plotW = w - padL - 20, rowH = h / MAXROWS;

            // left: the FFT slice currently being added
            var cur = rows.length ? rows[rows.length - 1] : makeRow(0);
            ctx.strokeStyle = C.grid;
            ctx.beginPath(); ctx.moveTo(20, h - 24); ctx.lineTo(padL - 34, h - 24); ctx.stroke();
            ctx.strokeStyle = C.accent; ctx.lineWidth = 1.6;
            ctx.beginPath();
            for (var i = 0; i < cur.length; i++) {
                var x = 20 + (i / cur.length) * (padL - 56);
                var y = (h - 24) - Math.min(1, cur[i]) * (h - 70);
                i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
            }
            ctx.stroke();
            ctx.fillStyle = C.dim; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText("one FFT: energy vs frequency", 20, 16);
            ctx.fillText("frequency →", 20, h - 8);

            // right: stacked into an image
            rows.forEach(function (r, ri) {
                for (var b = 0; b < r.length; b++) {
                    var v = Math.min(1, r[b]);
                    var shade = Math.floor(v * 255);
                    ctx.fillStyle = "rgb(" + Math.floor(shade * 0.35) + "," +
                                    Math.floor(shade * 0.75) + "," + shade + ")";
                    ctx.fillRect(padL + (b / r.length) * plotW, ri * rowH,
                                 plotW / r.length + 0.6, rowH + 0.6);
                }
            });
            ctx.fillStyle = C.dim;
            ctx.fillText("stacked over time → a picture the AI can read", padL, 16);
            ctx.save();
            ctx.translate(padL - 10, h / 2); ctx.rotate(-Math.PI / 2);
            ctx.fillText("time ↓", 0, 0); ctx.restore();

            requestAnimationFrame(draw);
        }
        draw();
    })();

    // ── 4a. one training step ────────────────────────────────────────────
    (function trainStep() {
        var svg = d3.select("#s-train");
        if (svg.empty()) return;
        var classes = ["WFM", "FM", "OOK", "MSK", "FSK", "APRS"];
        var truth = 0, guess = 0, auto = null;

        var stages = [
            { x: 70, label: "flashcard" }, { x: 300, label: "network" },
            { x: 530, label: "its guess" }, { x: 760, label: "correct?" }
        ];
        stages.forEach(function (s) {
            svg.append("text").attr("x", s.x).attr("y", 20).attr("text-anchor", "middle")
               .attr("fill", C.dim).style("font", "11px ui-monospace").text(s.label);
        });
        svg.append("rect").attr("x", 30).attr("y", 36).attr("width", 80).attr("height", 62)
           .attr("rx", 6).attr("fill", "#1c2430").attr("stroke", C.border || "#30363d");
        var cardTxt = svg.append("text").attr("x", 70).attr("y", 72).attr("text-anchor", "middle")
           .attr("fill", C.yellow).style("font", "13px ui-monospace");
        svg.append("rect").attr("x", 250).attr("y", 36).attr("width", 100).attr("height", 62)
           .attr("rx", 6).attr("fill", "#1c2430").attr("stroke", C.accent);
        svg.append("text").attr("x", 300).attr("y", 72).attr("text-anchor", "middle")
           .attr("fill", C.accent).style("font", "12px ui-monospace").text("CNN");
        var guessTxt = svg.append("text").attr("x", 530).attr("y", 72).attr("text-anchor", "middle")
           .style("font", "13px ui-monospace");
        var verdict = svg.append("text").attr("x", 760).attr("y", 72).attr("text-anchor", "middle")
           .style("font", "13px ui-monospace");
        [[115, 245], [355, 500], [560, 730]].forEach(function (p) {
            svg.append("line").attr("x1", p[0]).attr("y1", 67).attr("x2", p[1]).attr("y2", 67)
               .attr("stroke", C.grid).attr("marker-end", "");
            svg.append("path").attr("d", "M" + (p[1] - 6) + ",63 L" + p[1] + ",67 L" + (p[1] - 6) + ",71")
               .attr("fill", C.grid);
        });
        var accTxt = svg.append("text").attr("x", 430).attr("y", 130).attr("text-anchor", "middle")
           .attr("fill", C.dim).style("font", "11px ui-monospace");
        var seen = 0, right = 0;

        function step() {
            truth = Math.floor(Math.random() * classes.length);
            var skill = Math.min(0.93, seen / 40);
            guess = Math.random() < skill ? truth
                  : Math.floor(Math.random() * classes.length);
            cardTxt.text(classes[truth]);
            guessTxt.attr("fill", guess === truth ? C.green : C.red).text(classes[guess]);
            verdict.attr("fill", guess === truth ? C.green : C.red)
                   .text(guess === truth ? "✓ keep" : "✗ nudge");
            seen++; if (guess === truth) right++;
            accTxt.text("cards seen " + seen + "   •   correct " +
                        Math.round(100 * right / seen) + "%");
        }
        document.getElementById("train-step").addEventListener("click", step);
        document.getElementById("train-auto").addEventListener("click", function () {
            if (auto) { clearInterval(auto); auto = null; this.textContent = "run"; }
            else { auto = setInterval(step, 420); this.textContent = "pause"; }
        });
        step();
    })();

    // ── 4b. learning curves ──────────────────────────────────────────────
    (function curves() {
        var svg = d3.select("#s-curve");
        if (svg.empty()) return;
        var W = 860, H = 260, m = { t: 18, r: 20, b: 34, l: 46 };
        var x = d3.scaleLinear([0, 30], [m.l, W - m.r]);
        var y = d3.scaleLinear([0.3, 1.0], [H - m.b, m.t]);
        svg.append("g").attr("transform", "translate(0," + (H - m.b) + ")")
           .call(d3.axisBottom(x).ticks(6)).attr("color", C.dim);
        svg.append("g").attr("transform", "translate(" + m.l + ",0)")
           .call(d3.axisLeft(y).ticks(5).tickFormat(d3.format(".0%"))).attr("color", C.dim);
        svg.append("text").attr("x", W / 2).attr("y", H - 4).attr("text-anchor", "middle")
           .attr("fill", C.dim).style("font", "11px ui-monospace").text("training rounds");

        var line = d3.line().x(function (d, i) { return x(i); }).y(function (d) { return y(d); })
                     .curve(d3.curveMonotoneX);
        var pTrain = svg.append("path").attr("fill", "none").attr("stroke", C.accent).attr("stroke-width", 2);
        var pVal = svg.append("path").attr("fill", "none").attr("stroke", C.green).attr("stroke-width", 2);
        var hint = document.getElementById("curve-hint");

        function series(kind) {
            var tr = [], va = [];
            for (var i = 0; i <= 30; i++) {
                tr.push(Math.min(0.999, 0.45 + 0.55 * (1 - Math.exp(-i / 5))));
                va.push(kind === "good"
                    ? Math.min(0.96, 0.42 + 0.54 * (1 - Math.exp(-i / 6)))
                    : 0.42 + 0.36 * (1 - Math.exp(-i / 4)) - Math.max(0, (i - 10)) * 0.016);
            }
            return [tr, va];
        }
        function render(kind) {
            var s = series(kind);
            pTrain.datum(s[0]).transition().duration(600).attr("d", line);
            pVal.datum(s[1]).transition().duration(600).attr("d", line);
            hint.innerHTML = kind === "good"
                ? "Both climb together. It is learning something real."
                : "<strong>Memorising.</strong> It keeps getting better on cards it has " +
                  "already seen, while getting <em>worse</em> on new ones — it has " +
                  "learned the answer key, not the subject.";
        }
        wire("[data-curve]", function (d) { render(d.curve); });
        render("good");
    })();

    // ── 5a. the classroom confound ───────────────────────────────────────
    (function confound() {
        var svg = d3.select("#s-confound");
        if (svg.empty()) return;
        var rooms = [
            { x: 70, room: "94.9 MHz", sub: "only ever WFM", col: C.accent },
            { x: 300, room: "131.55 MHz", sub: "only ever ACARS", col: C.green },
            { x: 530, room: "345 MHz", sub: "only ever OOK", col: C.yellow },
            { x: 760, room: "152 MHz", sub: "only ever pager", col: C.purple }
        ];
        rooms.forEach(function (r) {
            svg.append("rect").attr("x", r.x - 68).attr("y", 30).attr("width", 136)
               .attr("height", 74).attr("rx", 8).attr("fill", "#1c2430").attr("stroke", r.col);
            svg.append("text").attr("x", r.x).attr("y", 60).attr("text-anchor", "middle")
               .attr("fill", r.col).style("font", "13px ui-monospace").text(r.room);
            svg.append("text").attr("x", r.x).attr("y", 82).attr("text-anchor", "middle")
               .attr("fill", C.dim).style("font", "11px ui-monospace").text(r.sub);
        });
        svg.append("text").attr("x", 430).attr("y", 140).attr("text-anchor", "middle")
           .attr("fill", C.red).style("font", "13px ui-monospace")
           .text("the model can score 100% by learning the ROOM, not the lesson");
        svg.append("text").attr("x", 430).attr("y", 168).attr("text-anchor", "middle")
           .attr("fill", C.green).style("font", "12px ui-monospace")
           .text("fix: teach the same lesson in many rooms — FM now comes from 17 frequencies");
    })();

    // ── 5b. live class balance ───────────────────────────────────────────
    (function balance() {
        var svg = d3.select("#s-balance");
        if (svg.empty()) return;
        var W = 860, H = 250, m = { t: 16, r: 20, b: 40, l: 76 };
        var hint = document.getElementById("balance-hint");
        var gx = svg.append("g").attr("transform", "translate(0," + (H - m.b) + ")");
        var gy = svg.append("g").attr("transform", "translate(" + m.l + ",0)");

        function render(counts) {
            var data = Object.keys(counts).map(function (k) { return { k: k, v: counts[k] }; })
                             .sort(function (a, b) { return b.v - a.v; });
            if (!data.length) { hint.textContent = "No samples collected yet."; return; }
            var x = d3.scaleBand(data.map(function (d) { return d.k; }), [m.l, W - m.r]).padding(0.28);
            var y = d3.scaleLinear([0, d3.max(data, function (d) { return d.v; })], [H - m.b, m.t]);
            gx.transition().duration(500).call(d3.axisBottom(x)).attr("color", C.dim);
            gy.transition().duration(500).call(d3.axisLeft(y).ticks(5)).attr("color", C.dim);

            var top = data[0].v, bars = svg.selectAll("rect.bar").data(data, function (d) { return d.k; });
            bars.enter().append("rect").attr("class", "bar")
                .attr("y", H - m.b).attr("height", 0)
              .merge(bars).transition().duration(600)
                .attr("x", function (d) { return x(d.k); })
                .attr("width", x.bandwidth())
                .attr("y", function (d) { return y(d.v); })
                .attr("height", function (d) { return H - m.b - y(d.v); })
                .attr("fill", function (d) { return d.v < top * 0.25 ? C.red : C.accent; });
            bars.exit().remove();

            var labs = svg.selectAll("text.val").data(data, function (d) { return d.k; });
            labs.enter().append("text").attr("class", "val").attr("text-anchor", "middle")
                .style("font", "11px ui-monospace")
              .merge(labs).transition().duration(600)
                .attr("x", function (d) { return x(d.k) + x.bandwidth() / 2; })
                .attr("y", function (d) { return y(d.v) - 6; })
                .attr("fill", function (d) { return d.v < top * 0.25 ? C.red : C.dim; })
                .text(function (d) { return d.v; });
            labs.exit().remove();

            var ratio = top / Math.max(1, d3.min(data, function (d) { return d.v; }));
            hint.innerHTML = "Live from this node. Largest class is <strong>" +
                ratio.toFixed(1) + "×</strong> the smallest" +
                (ratio > 4 ? " — <span style='color:" + C.red +
                 "'>too skewed to train on yet</span>." : " — workable.");
        }

        function poll() {
            fetch("/api/iq-collect").then(function (r) { return r.json(); })
              .then(function (d) {
                  render((d.corpus && d.corpus.per_class) || {});
                  var f = document.getElementById("foot-stats");
                  if (f) f.textContent = "corpus " + ((d.corpus && d.corpus.total) || 0) +
                      " samples • " + (d.capturing ? "collecting now" : "idle");
              })
              .catch(function () { hint.textContent = "Console offline — cannot read live corpus."; });
        }
        poll(); setInterval(poll, 10000);
    })();

    // ── 6. pipeline ──────────────────────────────────────────────────────
    (function pipeline() {
        var svg = d3.select("#s-pipeline");
        if (svg.empty()) return;
        var steps = [
            { x: 90, t: "collect", s: "rtl_sdr raw IQ", w: "Raspberry Pi", col: C.accent,
              d: "The dongle streams 2.4 million I/Q pairs a second. Detected transmissions are saved, labelled by whichever preset is tuned." },
            { x: 285, t: "label", s: "preset = truth", w: "Raspberry Pi", col: C.accent,
              d: "Tuned to 94.9 MHz means the samples ARE WFM. Free ground truth — provided the antenna can actually hear that band." },
            { x: 480, t: "train", s: "MobileNetV2", w: "x86 VM", col: C.yellow,
              d: "Fine-tune a network that already knows how to see. Cannot run on the Pi: needs PyTorch and real CPU." },
            { x: 675, t: "compile", s: "ONNX → .hef", w: "x86 VM", col: C.yellow,
              d: "Hailo's compiler quantises the model for the NPU. x86 only — this is why the Pi cannot do it." },
            { x: 800, t: "infer", s: "Hailo-8L", w: "Raspberry Pi", col: C.green,
              d: "The finished .hef runs on the NPU at a few milliseconds per classification, alongside Whisper." }
        ];
        var det = svg.append("text").attr("x", 430).attr("y", 205).attr("text-anchor", "middle")
            .attr("fill", C.dim).style("font", "12px ui-monospace")
            .text("hover a stage");

        steps.forEach(function (st, i) {
            var g = svg.append("g").style("cursor", "pointer");
            g.append("rect").attr("x", st.x - 62).attr("y", 54).attr("width", 124).attr("height", 68)
             .attr("rx", 8).attr("fill", "#1c2430").attr("stroke", st.col).attr("stroke-width", 1.4);
            g.append("text").attr("x", st.x).attr("y", 78).attr("text-anchor", "middle")
             .attr("fill", st.col).style("font", "13px ui-monospace").text(st.t);
            g.append("text").attr("x", st.x).attr("y", 97).attr("text-anchor", "middle")
             .attr("fill", C.text).style("font", "10px ui-monospace").text(st.s);
            g.append("text").attr("x", st.x).attr("y", 113).attr("text-anchor", "middle")
             .attr("fill", C.dim).style("font", "9px ui-monospace").text(st.w);
            g.on("mouseenter", function () {
                det.text(st.d.length > 96 ? st.d.slice(0, 96) + "…" : st.d);
                det.attr("fill", st.col);
            });
            g.append("title").text(st.d);
            if (i < steps.length - 1) {
                var x1 = st.x + 64, x2 = steps[i + 1].x - 64;
                svg.append("line").attr("x1", x1).attr("y1", 88).attr("x2", x2).attr("y2", 88)
                   .attr("stroke", C.grid).attr("stroke-width", 1.5);
                svg.append("path").attr("d", "M" + (x2 - 6) + ",84 L" + x2 + ",88 L" + (x2 - 6) + ",92")
                   .attr("fill", C.grid);
            }
        });
        svg.append("text").attr("x", 187).attr("y", 34).attr("text-anchor", "middle")
           .attr("fill", C.accent).style("font", "11px ui-monospace").text("─ on the Pi ─");
        svg.append("text").attr("x", 578).attr("y", 34).attr("text-anchor", "middle")
           .attr("fill", C.yellow).style("font", "11px ui-monospace").text("─ off-node (x86) ─");
    })();

    // ── 7a. transfer learning ────────────────────────────────────────────
    (function transfer() {
        var svg = d3.select("#s-transfer");
        if (svg.empty()) return;
        var blocks = [
            { x: 40,  w: 105, t: "edges",    keep: true },
            { x: 155, w: 105, t: "textures", keep: true },
            { x: 270, w: 105, t: "shapes",   keep: true },
            { x: 385, w: 105, t: "parts",    keep: true },
            { x: 500, w: 105, t: "objects",  keep: true },
            { x: 632, w: 150, t: "1000 objects", keep: false }
        ];
        blocks.forEach(function (b) {
            svg.append("rect").attr("x", b.x).attr("y", 52).attr("width", b.w)
               .attr("height", 52).attr("rx", 6)
               .attr("fill", b.keep ? "#16261c" : "#2a1a1a")
               .attr("stroke", b.keep ? C.green : C.red)
               .attr("stroke-dasharray", b.keep ? "none" : "4 3");
            svg.append("text").attr("x", b.x + b.w / 2).attr("y", 82)
               .attr("text-anchor", "middle")
               .attr("fill", b.keep ? C.green : C.red)
               .style("font", "11px ui-monospace").text(b.t);
        });
        svg.append("text").attr("x", 300).attr("y", 34).attr("text-anchor", "middle")
           .attr("fill", C.green).style("font", "12px ui-monospace")
           .text("KEPT — already knows how to look at a picture");
        svg.append("text").attr("x", 707).attr("y", 34).attr("text-anchor", "middle")
           .attr("fill", C.red).style("font", "12px ui-monospace").text("THROWN AWAY");
        svg.append("rect").attr("x", 632).attr("y", 112).attr("width", 150).attr("height", 40)
           .attr("rx", 6).attr("fill", "#16202e").attr("stroke", C.accent);
        svg.append("text").attr("x", 707).attr("y", 137).attr("text-anchor", "middle")
           .attr("fill", C.accent).style("font", "11px ui-monospace")
           .text("FM / WFM / OOK / MSK …");
        svg.append("path").attr("d", "M707,104 L707,112").attr("stroke", C.accent);
        svg.append("text").attr("x", 300).attr("y", 137).attr("text-anchor", "middle")
           .attr("fill", C.dim).style("font", "11px ui-monospace")
           .text("2.2 million weights, borrowed from a million photographs");
    })();

    // ── 7b. gradient descent ─────────────────────────────────────────────
    (function descent() {
        var ctx = ctxOf("c-descent");
        if (!ctx) return;
        var lr = "ok", ball = null, hint = document.getElementById("descent-hint");

        // an error "landscape" — the network is looking for the lowest point
        function loss(x) {
            return 0.5 + 0.42 * Math.sin(x * 2.1) * Math.exp(-Math.abs(x) * 0.25)
                       + 0.05 * x * x;
        }
        function slope(x) { return (loss(x + 0.01) - loss(x - 0.01)) / 0.02; }

        wire("[data-lr]", function (d) {
            lr = d.lr;
            hint.innerHTML = lr === "big"
                ? "<strong>Too big.</strong> Each step overshoots the bottom and it " +
                  "bounces around, never settling — in training this shows up as a " +
                  "loss that jumps about instead of falling."
                : "The 'learning rate' is how far it moves each step. Too big and it " +
                  "bounces straight over the bottom.";
            drop();
        });
        function drop() { ball = { x: -3.2 + Math.random() * 0.6, v: 0, done: false }; }
        var go = document.getElementById("descent-go");
        if (go) go.addEventListener("click", drop);
        drop();

        function draw() {
            var w = ctx._w, h = ctx._h, padL = 40, padB = 34;
            ctx.clearRect(0, 0, w, h);
            var X = function (x) { return padL + (x + 4) / 8 * (w - padL - 20); };
            var Y = function (v) { return h - padB - v * (h - padB - 26) / 1.4; };

            ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(padL, h - padB); ctx.lineTo(w - 20, h - padB); ctx.stroke();
            ctx.fillStyle = C.dim; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText("a weight's value →", padL, h - 12);
            ctx.save(); ctx.translate(16, h / 2); ctx.rotate(-Math.PI / 2);
            ctx.fillText("error", 0, 0); ctx.restore();

            ctx.strokeStyle = C.accent; ctx.lineWidth = 2;
            ctx.beginPath();
            for (var px = -4; px <= 4; px += 0.02) {
                var x = X(px), y = Y(loss(px));
                px === -4 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
            }
            ctx.stroke();

            if (ball) {
                var step = lr === "big" ? 0.62 : 0.12;
                if (!ball.done) {
                    var g = slope(ball.x);
                    ball.x -= step * g;                     // gradient descent, literally
                    if (Math.abs(g) < 0.004 && lr !== "big") ball.done = true;
                    if (ball.x < -4) ball.x = -4; if (ball.x > 4) ball.x = 4;
                }
                var bx = X(ball.x), by = Y(loss(ball.x));
                ctx.fillStyle = ball.done ? C.green : C.yellow;
                ctx.beginPath(); ctx.arc(bx, by - 6, 7, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = C.dim;
                ctx.fillText(ball.done ? "settled at the bottom" :
                             (lr === "big" ? "overshooting" : "rolling downhill"), bx - 40, by - 22);
            }
            requestAnimationFrame(draw);
        }
        draw();
    })();

    // ── 8a. feed-forward network, using OUR classes ──────────────────────
    (function ffnet() {
        var svg = d3.select("#s-ffnet");
        if (svg.empty()) return;
        var CLASSES = ["FM", "WFM", "OOK", "MSK", "FSK", "AFSK1200"];
        var layers = [
            { n: 8, x: 95,  label: "spectrogram pixels", sub: "224 x 224" },
            { n: 6, x: 300, label: "edges / textures", sub: "borrowed layers" },
            { n: 6, x: 505, label: "signal shapes", sub: "borrowed layers" },
            { n: 6, x: 720, label: "our classes", sub: "retrained head" }
        ];
        var hint = document.getElementById("ff-hint");
        var nodes = [], links = [];

        layers.forEach(function (L, li) {
            var gap = 250 / (L.n + 1);
            for (var i = 0; i < L.n; i++) {
                nodes.push({ li: li, i: i, x: L.x, y: 40 + gap * (i + 1) });
            }
            svg.append("text").attr("x", L.x).attr("y", 20).attr("text-anchor", "middle")
               .attr("fill", li === 3 ? C.accent : C.dim)
               .style("font", "11px ui-monospace").text(L.label);
            svg.append("text").attr("x", L.x).attr("y", 33).attr("text-anchor", "middle")
               .attr("fill", C.grid).style("font", "9px ui-monospace").text(L.sub);
        });

        for (var li = 0; li < layers.length - 1; li++) {
            nodes.filter(function (n) { return n.li === li; }).forEach(function (a) {
                nodes.filter(function (n) { return n.li === li + 1; }).forEach(function (b) {
                    links.push({ a: a, b: b, w: Math.random() });
                });
            });
        }

        var lsel = svg.selectAll("line.lnk").data(links).enter().append("line")
            .attr("class", "lnk")
            .attr("x1", function (d) { return d.a.x; }).attr("y1", function (d) { return d.a.y; })
            .attr("x2", function (d) { return d.b.x; }).attr("y2", function (d) { return d.b.y; })
            .attr("stroke", C.grid)
            .attr("stroke-width", function (d) { return 0.3 + d.w * 1.5; });

        var nsel = svg.selectAll("circle.nd").data(nodes).enter().append("circle")
            .attr("class", "nd")
            .attr("cx", function (d) { return d.x; }).attr("cy", function (d) { return d.y; })
            .attr("r", 8).attr("fill", "#1c2430").attr("stroke", C.grid);

        // label the output neurons with the modulations this node actually collects
        nodes.filter(function (n) { return n.li === 3; }).forEach(function (n, i) {
            svg.append("text").attr("x", n.x + 16).attr("y", n.y + 4)
               .attr("fill", C.dim).style("font", "10px ui-monospace").text(CLASSES[i]);
        });

        var busy = false, auto = null;
        function pulse(dir) {
            if (busy) return;
            busy = true;
            var order = dir > 0 ? [0, 1, 2, 3] : [3, 2, 1, 0];
            var winner = Math.floor(Math.random() * CLASSES.length);
            hint.innerHTML = dir > 0
                ? "Forward: each neuron multiplies its inputs by its weights, sums them, "
                  + "applies ReLU. The brightest output neuron wins &mdash; here <strong>"
                  + CLASSES[winner] + "</strong>."
                : "Backward: the error is pushed back through the same connections. Each "
                  + "weight learns how much IT contributed, and moves a small step to "
                  + "reduce it. That is backpropagation.";
            order.forEach(function (li, step) {
                setTimeout(function () {
                    nsel.filter(function (d) { return d.li === li; })
                        .transition().duration(160)
                        .attr("fill", dir > 0 ? C.accent : C.yellow).attr("r", 10)
                        .transition().duration(320)
                        .attr("fill", function (d) {
                            return (dir > 0 && li === 3 && d.i === winner) ? C.green : "#1c2430";
                        })
                        .attr("r", function (d) {
                            return (dir > 0 && li === 3 && d.i === winner) ? 11 : 8;
                        });
                    lsel.filter(function (d) {
                        return dir > 0 ? d.a.li === li : d.b.li === li;
                    }).transition().duration(160)
                      .attr("stroke", dir > 0 ? C.accent : C.yellow)
                      .transition().duration(420).attr("stroke", C.grid);
                    if (step === 3) setTimeout(function () { busy = false; }, 500);
                }, step * 380);
            });
        }
        document.getElementById("ff-forward").addEventListener("click", function () { pulse(1); });
        document.getElementById("ff-back").addEventListener("click", function () { pulse(-1); });
        document.getElementById("ff-auto").addEventListener("click", function () {
            if (auto) { clearInterval(auto); auto = null; this.textContent = "run continuously"; }
            else {
                this.textContent = "stop";
                var fwd = true;
                auto = setInterval(function () { pulse(fwd ? 1 : -1); fwd = !fwd; }, 1900);
            }
        });
        pulse(1);
    })();

    // ── 8b. activation functions ─────────────────────────────────────────
    (function activation() {
        var ctx = ctxOf("c-activation");
        if (!ctx) return;
        var kind = "relu", hint = document.getElementById("act-hint");
        var notes = {
            relu: "max(0, x). Cheap, and does not flatten out for large inputs — the " +
                  "default in MobileNetV2 (which actually uses ReLU6, clipped at 6 to " +
                  "stay friendly to 8-bit quantisation on chips like the Hailo).",
            sigmoid: "Squashes everything into 0..1. Its slope vanishes at both ends, so " +
                     "gradients die in deep stacks — this is why deep nets moved away from it.",
            tanh: "Like sigmoid but centred on zero, which helps. Still saturates.",
            linear: "No activation at all. Stack a hundred of these and you still only " +
                    "have a linear function — depth buys you nothing. This is WHY " +
                    "non-linearity is required."
        };
        wire("[data-act]", function (d) { kind = d.act; hint.textContent = notes[kind]; });
        hint.textContent = notes.relu;

        function f(x) {
            if (kind === "relu") return Math.max(0, Math.min(6, x));   // ReLU6
            if (kind === "sigmoid") return 1 / (1 + Math.exp(-x));
            if (kind === "tanh") return Math.tanh(x);
            return x;
        }
        function draw() {
            var w = ctx._w, h = ctx._h, cx = w / 2, cy = h / 2 + 20;
            ctx.clearRect(0, 0, w, h);
            ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(40, cy); ctx.lineTo(w - 20, cy);
            ctx.moveTo(cx, 16); ctx.lineTo(cx, h - 12); ctx.stroke();
            ctx.fillStyle = C.dim; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText("input", w - 60, cy - 6); ctx.fillText("output", cx + 6, 24);

            ctx.strokeStyle = C.accent; ctx.lineWidth = 2.2;
            ctx.beginPath();
            for (var px = 40; px < w - 20; px++) {
                var x = (px - cx) / 42, y = cy - f(x) * 26;
                y = Math.max(14, Math.min(h - 10, y));
                px === 40 ? ctx.moveTo(px, y) : ctx.lineTo(px, y);
            }
            ctx.stroke();
            requestAnimationFrame(draw);
        }
        draw();
    })();

    // ── 9a. convolution over a SPECTROGRAM ───────────────────────────────
    (function convolution() {
        var ctx = ctxOf("c-conv");
        if (!ctx) return;
        var N = 34, img = [], kern = "edge", pos = 0, out = [];

        // synthesise something that looks like OUR data: a steady carrier plus a burst
        for (var r = 0; r < N; r++) {
            img.push([]);
            for (var c = 0; c < N; c++) {
                var v = 0.13 + Math.random() * 0.09;                  // noise floor
                if (Math.abs(c - 11) < 2) v += 0.72;                  // continuous carrier
                if (r > 12 && r < 19 && Math.abs(c - 23) < 4) v += 0.66; // a burst
                img[r].push(Math.min(1, v));
            }
        }
        var kernels = {
            edge:  [[-1,-1,-1],[-1,8,-1],[-1,-1,-1]],
            horiz: [[-1,-1,-1],[2,2,2],[-1,-1,-1]],
            blur:  [[1,1,1],[1,1,1],[1,1,1]].map(function(r){return r.map(function(v){return v/9;});})
        };
        var notes = {
            edge: "Fires where brightness changes sharply — it finds the EDGES of the " +
                  "carrier and the burst, ignoring flat noise.",
            horiz: "Tuned to horizontal structure — in a spectrogram that means events " +
                   "spread across frequency, like a wideband burst.",
            blur: "Averages its neighbours. Smooths noise away, but blurs the very edges " +
                  "the first kernel was looking for."
        };
        var hint = document.getElementById("conv-hint");
        wire("[data-kern]", function (d) { kern = d.kern; out = []; pos = 0;
            hint.innerHTML = notes[kern] + " <strong>Nine weights, reused everywhere.</strong>"; });

        function draw() {
            var w = ctx._w, h = ctx._h, cell = 6, ox = 30, oy = 26;
            ctx.clearRect(0, 0, w, h);
            for (var r = 0; r < N; r++) for (var c = 0; c < N; c++) {
                var v = Math.floor(img[r][c] * 255);
                ctx.fillStyle = "rgb(" + Math.floor(v*0.3) + "," + Math.floor(v*0.7) + "," + v + ")";
                ctx.fillRect(ox + c*cell, oy + r*cell, cell, cell);
            }
            ctx.fillStyle = C.dim; ctx.font = "10px ui-monospace, monospace";
            ctx.fillText("input spectrogram", ox, 18);

            var kr = Math.floor(pos / (N-2)), kc = pos % (N-2);
            ctx.strokeStyle = C.yellow; ctx.lineWidth = 2;
            ctx.strokeRect(ox + kc*cell, oy + kr*cell, cell*3, cell*3);

            // the 3x3 kernel, drawn large
            var kx = ox + N*cell + 40, ky = oy + 20;
            ctx.fillStyle = C.dim; ctx.fillText("kernel (9 weights)", kx, 18);
            var K = kernels[kern];
            for (var i = 0; i < 3; i++) for (var j = 0; j < 3; j++) {
                ctx.strokeStyle = C.grid; ctx.lineWidth = 1;
                ctx.strokeRect(kx + j*26, ky + i*26, 26, 26);
                ctx.fillStyle = K[i][j] > 0 ? C.green : C.red;
                ctx.font = "10px ui-monospace, monospace";
                ctx.fillText(K[i][j].toFixed(K[i][j] % 1 ? 2 : 0), kx + j*26 + 5, ky + i*26 + 17);
            }

            // accumulate the output map
            var acc = 0;
            for (var a = 0; a < 3; a++) for (var b = 0; b < 3; b++)
                acc += img[kr+a][kc+b] * K[a][b];
            if (!out[kr]) out[kr] = [];
            out[kr][kc] = Math.abs(acc);

            var oxx = kx + 110;
            ctx.fillStyle = C.dim; ctx.fillText("feature map", oxx, 18);
            for (var rr = 0; rr < out.length; rr++)
                for (var cc = 0; cc < (out[rr] || []).length; cc++) {
                    var vv = Math.floor(Math.min(1, out[rr][cc]) * 255);
                    ctx.fillStyle = "rgb(" + vv + "," + Math.floor(vv*0.8) + ",40)";
                    ctx.fillRect(oxx + cc*cell, oy + rr*cell, cell, cell);
                }

            pos = (pos + 1) % ((N-2) * (N-2));
            if (pos === 0) out = [];
            requestAnimationFrame(draw);
        }
        draw();
    })();

    // ── 9b. depthwise separable convolution ──────────────────────────────
    (function depthwise() {
        var svg = d3.select("#s-depthwise");
        if (svg.empty()) return;

        function block(x, y, w, h, fill, stroke, label, sub) {
            svg.append("rect").attr("x", x).attr("y", y).attr("width", w).attr("height", h)
               .attr("rx", 5).attr("fill", fill).attr("stroke", stroke);
            svg.append("text").attr("x", x + w/2).attr("y", y + h/2 + 1)
               .attr("text-anchor", "middle").attr("fill", stroke)
               .style("font", "11px ui-monospace").text(label);
            if (sub) svg.append("text").attr("x", x + w/2).attr("y", y + h/2 + 15)
               .attr("text-anchor", "middle").attr("fill", C.dim)
               .style("font", "9px ui-monospace").text(sub);
        }

        svg.append("text").attr("x", 20).attr("y", 22).attr("fill", C.red)
           .style("font", "12px ui-monospace").text("standard convolution");
        block(20, 34, 250, 46, "#2a1a1a", C.red, "3x3 across ALL channels", "every input x every output");
        svg.append("text").attr("x", 285).attr("y", 62).attr("fill", C.red)
           .style("font", "12px ui-monospace").text("3 x 3 x 32 x 64  =  18,432 multiplies");

        svg.append("text").attr("x", 20).attr("y", 116).attr("fill", C.green)
           .style("font", "12px ui-monospace").text("depthwise separable (MobileNetV2)");
        block(20, 128, 120, 46, "#16261c", C.green, "3x3 per channel", "filter only");
        block(150, 128, 120, 46, "#16261c", C.green, "1x1 mix", "combine only");
        svg.append("path").attr("d", "M141,151 L149,151").attr("stroke", C.green);
        svg.append("text").attr("x", 285).attr("y", 156).attr("fill", C.green)
           .style("font", "12px ui-monospace")
           .text("3 x 3 x 32  +  32 x 64  =  2,336 multiplies");

        svg.append("text").attr("x", 430).attr("y", 196).attr("text-anchor", "middle")
           .attr("fill", C.accent).style("font", "13px ui-monospace")
           .text("~8x less work for almost the same accuracy — this is why it fits on the Pi");
    })();

    // ── 9c. the real layer stack (numbers dumped from our actual model) ──
    (function layerStack() {
        var svg = d3.select("#s-layers");
        if (svg.empty()) return;
        var hint = document.getElementById("layers-hint");
        var stack = [
            { n: "input",   sp: 224, ch: 3,    p: 0,       d: "the spectrogram, greyscale repeated into 3 channels because the borrowed network expects RGB" },
            { n: "conv 0",  sp: 112, ch: 32,   p: 928,     d: "first convolution. Halves the image, produces 32 edge-detector responses" },
            { n: "1-3",     sp: 56,  ch: 24,   p: 14864,   d: "InvertedResidual x3 — simple textures at half resolution again" },
            { n: "4-6",     sp: 28,  ch: 32,   p: 39696,   d: "stripes and gradients. A carrier's vertical edge is found around here" },
            { n: "7-10",    sp: 14,  ch: 64,   p: 183872,  d: "combinations — 'narrow vertical band', 'wide horizontal smear'" },
            { n: "11-13",   sp: 14,  ch: 96,   p: 303168,  d: "whole-structure features; spatial size held, depth increased" },
            { n: "14-16",   sp: 7,   ch: 160,  p: 795264,  d: "only 7x7 pixels left. Almost all meaning, almost no detail" },
            { n: "17",      sp: 7,   ch: 320,  p: 473920,  d: "the widest feature set before the final expansion" },
            { n: "conv 18", sp: 7,   ch: 1280, p: 412160,  d: "1x1 expansion into the 1280-dimensional feature space" },
            { n: "pool",    sp: 1,   ch: 1280, p: 0,       d: "average each channel over space — 1280 numbers describing the whole image" },
            { n: "head",    sp: 1,   ch: 6,    p: 7686,    d: "Linear(1280, 6). THE ONLY PART WE TRAIN — one score per modulation" }
        ];
        var x0 = 34, gap = (860 - x0 - 30) / stack.length;
        var maxH = 190;

        stack.forEach(function (L, i) {
            var x = x0 + i * gap;
            var h = Math.max(9, maxH * Math.sqrt(L.sp / 224));
            var y = 40 + (maxH - h) / 2;
            var depth = Math.min(1, Math.log(L.ch + 1) / Math.log(1281));
            var col = L.n === "head" ? C.accent
                    : d3.interpolateRgb("#1e3a5f", C.purple)(depth);
            var g = svg.append("g").style("cursor", "pointer");
            g.append("rect").attr("x", x).attr("y", y)
             .attr("width", gap - 7).attr("height", h).attr("rx", 3)
             .attr("fill", col).attr("stroke", L.n === "head" ? C.accent : C.grid);
            g.append("text").attr("x", x + (gap - 7) / 2).attr("y", 254)
             .attr("text-anchor", "middle").attr("fill", C.dim)
             .style("font", "9px ui-monospace").text(L.n);
            g.append("text").attr("x", x + (gap - 7) / 2).attr("y", 268)
             .attr("text-anchor", "middle").attr("fill", C.grid)
             .style("font", "8px ui-monospace")
             .text(L.sp > 1 ? L.sp + "²" : "");
            g.append("text").attr("x", x + (gap - 7) / 2).attr("y", 281)
             .attr("text-anchor", "middle").attr("fill", C.grid)
             .style("font", "8px ui-monospace").text(L.ch + "ch");
            g.on("mouseenter", function () {
                hint.innerHTML = "<strong>" + L.n + "</strong> &mdash; " + L.d +
                    (L.p ? "  <span class='mono' style='color:" + C.dim + "'>(" +
                     L.p.toLocaleString() + " params)</span>" : "");
            });
            g.append("title").text(L.d);
        });

        svg.append("text").attr("x", 34).attr("y", 24).attr("fill", C.dim)
           .style("font", "10px ui-monospace").text("spatial size shrinks →");
        svg.append("text").attr("x", 430).attr("y", 310).attr("text-anchor", "middle")
           .attr("fill", C.dim).style("font", "11px ui-monospace")
           .text("2,231,558 parameters total  •  7,686 retrained (0.3%)");
    })();

    // ── 9d. one InvertedResidual block ───────────────────────────────────
    (function invRes() {
        var svg = d3.select("#s-invres");
        if (svg.empty()) return;
        function bar(x, y, w, h, fill, stroke, top, bottom) {
            svg.append("rect").attr("x", x).attr("y", y).attr("width", w).attr("height", h)
               .attr("rx", 4).attr("fill", fill).attr("stroke", stroke);
            svg.append("text").attr("x", x + w/2).attr("y", y - 8).attr("text-anchor", "middle")
               .attr("fill", stroke).style("font", "11px ui-monospace").text(top);
            if (bottom) svg.append("text").attr("x", x + w/2).attr("y", y + h + 16)
               .attr("text-anchor", "middle").attr("fill", C.dim)
               .style("font", "9px ui-monospace").text(bottom);
        }
        // heights encode channel count: narrow -> wide -> narrow
        bar(40,  78, 62, 44,  "#16202e", C.accent, "in", "24 ch");
        bar(160, 44, 62, 112, "#241a2e", C.purple, "expand 1x1", "144 ch");
        bar(280, 44, 62, 112, "#16261c", C.green,  "depthwise 3x3", "144 ch, 1 filter each");
        bar(400, 78, 62, 44,  "#241a2e", C.purple, "project 1x1", "back to 24 ch");
        bar(520, 78, 62, 44,  "#16202e", C.accent, "out", "24 ch");

        [[102,160],[222,280],[342,400],[462,520]].forEach(function (p) {
            svg.append("path").attr("d", "M" + p[0] + ",100 L" + (p[1]-6) + ",100")
               .attr("stroke", C.grid).attr("stroke-width", 1.4);
            svg.append("path").attr("d", "M" + (p[1]-6) + ",96 L" + p[1] + ",100 L" + (p[1]-6) + ",104")
               .attr("fill", C.grid);
        });

        // the skip connection
        svg.append("path").attr("d", "M71,78 C71,20 551,20 551,78")
           .attr("fill", "none").attr("stroke", C.yellow).attr("stroke-dasharray", "5 4");
        svg.append("text").attr("x", 311).attr("y", 22).attr("text-anchor", "middle")
           .attr("fill", C.yellow).style("font", "11px ui-monospace")
           .text("skip: add the input back (only when shapes match)");

        svg.append("text").attr("x", 660).attr("y", 88).attr("fill", C.green)
           .style("font", "11px ui-monospace").text("no ReLU after project —");
        svg.append("text").attr("x", 660).attr("y", 104).attr("fill", C.green)
           .style("font", "11px ui-monospace").text("a ReLU on a narrow layer");
        svg.append("text").attr("x", 660).attr("y", 120).attr("fill", C.green)
           .style("font", "11px ui-monospace").text("destroys information");
        svg.append("text").attr("x", 660).attr("y", 140).attr("fill", C.dim)
           .style("font", "10px ui-monospace").text("(\u201clinear bottleneck\u201d)");
    })();

    // ── 11. execution trace, using values captured from real runs ────────
    (function executionTrace() {
        var codeEl = document.getElementById("tr-code");
        if (!codeEl) return;
        var valsEl = document.getElementById("tr-vals"),
            noteEl = document.getElementById("tr-note"),
            posEl  = document.getElementById("tr-pos");
        var step = 0, auto = null, T = null, STEPS = [];

        function row(k, v, colour) {
            return "<div><span style='color:" + C.dim + "'>" + k +
                   "</span>  <span style='color:" + (colour || C.text) + "'>" +
                   v + "</span></div>";
        }
        function code(lines, hot) {
            return lines.map(function (l, i) {
                var on = i === hot;
                return "<div style='" + (on
                    ? "background:#1c2a3a;color:" + C.accent + ";border-left:2px solid " + C.accent + ";padding-left:6px"
                    : "color:" + C.dim + ";padding-left:8px") + "'>" +
                    l.replace(/&/g, "&amp;").replace(/</g, "&lt;") + "</div>";
            }).join("");
        }

        function build(t) {
            var d = t.dsp, tr = t.train;
            var fmt = function (a) { return "[" + a.join(", ") + "]"; };

            return [
            { title: "1 · the dongle hands over IQ",
              code: ["# rtl_sdr streams interleaved 8-bit I/Q",
                     "raw = proc.stdout.read(65536)",
                     "i   = raw[0::2].astype(float32) - 127.5",
                     "q   = raw[1::2].astype(float32) - 127.5",
                     "iq  = (i + 1j*q).astype(complex64)"],
              hot: 4,
              vals: row("captured", d.file, C.yellow) +
                    row("frequency", d.freq_mhz.toFixed(3) + " MHz") +
                    row("length", d.iq_len.toLocaleString() + " complex samples") +
                    row("iq[0..5]", d.iq.map(function (p) {
                        return "(" + p[0] + (p[1] < 0 ? "" : "+") + p[1] + "j)"; }).join(" "), C.accent),
              note: "Two numbers per sample, centred on 127.5. Nothing has been " +
                    "interpreted yet — this is just what came off the wire." },

            { title: "2 · the same points, read as magnitude and angle",
              code: ["magnitude = np.abs(iq)      # how strong",
                     "angle     = np.angle(iq)    # where in the circle"],
              hot: 0,
              vals: row("|iq|", fmt(d.mag), C.green) +
                    row("angle°", fmt(d.ang), C.purple) +
                    row("", "") +
                    row("note", "same six samples, polar instead of cartesian"),
              note: "Identical data. Magnitude carries AM, the rate of change of angle " +
                    "carries FM — which is why both numbers had to be recorded." },

            { title: "3 · window one frame, then FFT it",
              code: ["window = np.hanning(256)",
                     "frame  = iq[0:256] * window",
                     "fft    = np.fft.fftshift(np.fft.fft(frame))",
                     "power  = np.maximum(np.abs(fft)**2, 1e-20)",
                     "db     = 10 * np.log10(power)"],
              hot: 2,
              vals: row("hanning[0..5]", fmt(d.window_first), C.dim) +
                    row("|fft| bins 120-125", fmt(d.fft_mag), C.accent) +
                    row("dB", fmt(d.db), C.green),
              note: "The Hanning window tapers each frame to zero at its edges. Without " +
                    "it, chopping the signal creates artificial edges that the FFT " +
                    "reports as energy that was never on the air." },

            { title: "4 · stack every frame into a spectrogram",
              code: ["n_frames    = (len(iq) - 256) // 128 + 1",
                     "spectrogram = np.zeros((n_frames, 256))",
                     "for i in range(n_frames):",
                     "    spectrogram[i] = fft_of_frame(i)"],
              hot: 3,
              vals: row("frames", d.spec_shape[0] + "  (hop 128 across " +
                        d.iq_len.toLocaleString() + " samples)") +
                    row("shape", d.spec_shape[0] + " × " + d.spec_shape[1], C.accent) +
                    row("dB range", d.spec_min + " … " + d.spec_max, C.green) +
                    row("", "") +
                    row("duration", (d.iq_len / 2400) .toFixed(1) + " ms of radio"),
              note: "Now it is a picture: " + d.spec_shape[0] + " rows of time, 256 " +
                    "columns of frequency." },

            { title: "5 · squash it into the shape the network expects",
              code: ["lo, hi = spec.min(), spec.max()",
                     "img = ((spec - lo)/(hi - lo) * 255).astype(uint8)",
                     "img = img[np.ix_(rows, cols)]   # -> 224x224"],
              hot: 1,
              vals: row("input range", d.spec_min + " … " + d.spec_max + " dB") +
                    row("output range", "0 … 255", C.accent) +
                    row("shape", d.img_shape[0] + " × " + d.img_shape[1]) +
                    row("row 112", fmt(d.img_row), C.green),
              note: "Each image is scaled by its OWN min and max, so absolute signal " +
                    "strength is thrown away. That is deliberate — but it means pure " +
                    "noise gets stretched to full contrast, which is why a separate " +
                    "signal check is needed." },

            { title: "6 · is there actually a signal here?",
              code: ["ratio = spectral_peak_ratio(iq)",
                     "if ratio < 300:",
                     "    return None        # empty channel, do not collect"],
              hot: 0,
              vals: row("peak / median", d.peak_ratio.toLocaleString(), C.green) +
                    row("gate", d.gate) +
                    row("verdict", d.peak_ratio >= d.gate ? "KEEP" : "REJECT",
                        d.peak_ratio >= d.gate ? C.green : C.red) +
                    row("", "") +
                    row("for reference", "known noise ≈ 124, empty OOK ≈ 244", C.dim),
              note: "This gate is what stopped 89,460 empty windows entering the corpus." },

            { title: "7 · forward pass — the model guesses",
              code: ["outputs = model(images)     # a batch of 8",
                     "# raw scores, NOT probabilities"],
              hot: 0,
              vals: row("batch", tr.batch.join(", "), C.dim) +
                    row("true label", tr.true_label, C.yellow) +
                    row("logits", "[" + tr.logits.slice(0, 6).join(", ") + " …]", C.accent),
              note: "Fifteen numbers, one per class. They are unbounded and do not sum " +
                    "to anything — the network has not been asked for probabilities." },

            { title: "8 · softmax turns scores into probabilities",
              code: ["probs = exp(logits) / exp(logits).sum()",
                     "# CrossEntropyLoss does this internally"],
              hot: 0,
              vals: row("probs", "[" + tr.probs.slice(0, 6).map(function (p) {
                        return (p * 100).toFixed(1) + "%"; }).join(", ") + " …]", C.accent) +
                    row("sum", "100%") +
                    row("truth", tr.true_label + " — the model gave it " +
                        (tr.probs[tr.classes.indexOf(tr.true_label)] * 100).toFixed(1) + "%",
                        C.red),
              note: "Untrained, it is spreading its bet almost evenly — roughly 1/15 " +
                    "each. That is exactly what ignorance looks like." },

            { title: "9 · the loss — one number for 'how wrong'",
              code: ["loss = criterion(outputs, labels)",
                     "# -log(probability of the correct class)"],
              hot: 0,
              vals: row("loss", tr.loss, C.red) +
                    row("", "") +
                    row("if it were certain and right", "0.00", C.green) +
                    row("if it guessed evenly (1/15)", "2.71", C.dim) +
                    row("ours", tr.loss + "  → worse than guessing", C.red),
              note: "Cross-entropy is the negative log of the probability given to the " +
                    "right answer. Confident and wrong is punished far harder than unsure." },

            { title: "10 · backward — who caused the error?",
              code: ["optimizer.zero_grad()",
                     "loss.backward()",
                     "# gradients computed; NOTHING has moved yet"],
              hot: 1,
              vals: row("weights (6 of 1280)", fmt(tr.w_before)) +
                    row("gradients", fmt(tr.grads), C.yellow) +
                    row("gradient norm", tr.grad_norm) +
                    row("", "") +
                    row("weights now", fmt(tr.w_before) + "  ← unchanged", C.dim),
              note: "backward() only assigns blame. The chain rule walks the network in " +
                    "reverse working out each weight's contribution to that 3.09." },

            { title: "11 · step — apply the correction",
              code: ["optimizer.step()",
                     "# w  ←  w  −  lr × gradient"],
              hot: 0,
              vals: row("before", fmt(tr.w_before)) +
                    row("− lr(" + tr.lr + ") × grad", fmt(tr.grads.map(function (g) {
                        return Math.round(-tr.lr * g * 1e5) / 1e5; })), C.yellow) +
                    row("after", fmt(tr.w_after), C.green) +
                    row("", "") +
                    row("changed by", fmt(tr.w_after.map(function (a, i) {
                        return Math.round((a - tr.w_before[i]) * 1e5) / 1e5; })), C.accent),
              note: "Six of 2,231,558 weights, nudged. Repeat 600 times and the model " +
                    "has learned — that is 1.3 billion individual corrections." }
            ];
        }

        function render() {
            var st = STEPS[step];
            codeEl.innerHTML = code(st.code, st.hot);
            valsEl.innerHTML = "<div style='color:" + C.yellow + ";margin-bottom:8px'>" +
                               st.title + "</div>" + st.vals;
            noteEl.innerHTML = st.note;
            posEl.textContent = (step + 1) + " / " + STEPS.length;
        }
        function go(d) {
            step = (step + d + STEPS.length) % STEPS.length;
            render();
        }

        fetch("/static/trace.json").then(function (r) { return r.json(); })
          .then(function (t) {
              T = t; STEPS = build(t);
              var fe = document.getElementById("trace-file");
              if (fe) fe.textContent = t.dsp.file;
              render();
              document.getElementById("tr-next").addEventListener("click", function () { go(1); });
              document.getElementById("tr-prev").addEventListener("click", function () { go(-1); });
              document.getElementById("tr-play").addEventListener("click", function () {
                  if (auto) { clearInterval(auto); auto = null; this.textContent = "play"; }
                  else { this.textContent = "pause"; auto = setInterval(function () { go(1); }, 3200); }
              });
          })
          .catch(function () {
              codeEl.textContent = "trace.json unavailable";
          });
    })();

    // ── 12. the full cycle ───────────────────────────────────────────────
    (function cycle() {
        var svg = d3.select("#s-cycle");
        if (svg.empty()) return;
        var hint = document.getElementById("cycle-hint");
        var steps = [
            { x: 120, y: 70,  c: C.accent, t: "1 · collect", s: "Raspberry Pi",
              d: "rtl_sdr streams raw IQ. The segmenter finds transmissions, the preset supplies the label. 10,083 kept — and 89,460 empty windows rejected." },
            { x: 400, y: 70,  c: C.yellow, t: "2 · train", s: "x86 VM on Proxmox",
              d: "MobileNetV2 fine-tuned, 10 epochs on 4 cores. val 0.9987 — on a random split, which we already know overstates it." },
            { x: 680, y: 70,  c: C.yellow, t: "3 · export", s: "x86 VM",
              d: "PyTorch to ONNX, 8.6 MB. Took four unrelated dependency failures to get out — none of them about the model." },
            { x: 680, y: 190, c: C.green,  t: "4 · deploy", s: "back to the Pi",
              d: "onnxruntime runs it on the Pi CPU at 57.8 ms per classification. No Hailo compiler needed to start using it." },
            { x: 400, y: 190, c: C.green,  t: "5 · observe", s: "live",
              d: "The classifier now labels real transmissions as the collector rotates bands. Predictions from unvalidated classes are flagged in the UI." },
            { x: 120, y: 190, c: C.red,    t: "6 · find the flaw", s: "and go again",
              d: "Held-out-frequency testing showed 3 of 6 classes cannot be trusted. That sends us back to step 1 for more frequencies — which is the loop." }
        ];
        steps.forEach(function (st, i) {
            var g = svg.append("g").style("cursor", "pointer");
            g.append("rect").attr("x", st.x - 78).attr("y", st.y - 26).attr("width", 156)
             .attr("height", 56).attr("rx", 8).attr("fill", "#1c2430")
             .attr("stroke", st.c).attr("stroke-width", 1.4);
            g.append("text").attr("x", st.x).attr("y", st.y - 4).attr("text-anchor", "middle")
             .attr("fill", st.c).style("font", "12px ui-monospace").text(st.t);
            g.append("text").attr("x", st.x).attr("y", st.y + 14).attr("text-anchor", "middle")
             .attr("fill", C.dim).style("font", "9px ui-monospace").text(st.s);
            g.on("mouseenter", function () {
                hint.textContent = st.d; hint.style.color = st.c;
            });
            g.append("title").text(st.d);
        });
        // arrows round the loop
        [[198,70,322,70],[478,70,602,70],[680,30,680,164],
         [602,190,478,190],[322,190,198,190]].forEach(function (a, i) {
            var vertical = a[0] === a[2];
            svg.append("path")
               .attr("d", vertical ? "M" + a[0] + "," + (a[1]+66) + " L" + a[2] + "," + a[3]
                                   : "M" + a[0] + "," + a[1] + " L" + a[2] + "," + a[3])
               .attr("stroke", C.grid).attr("stroke-width", 1.5).attr("fill", "none");
        });
        svg.append("path").attr("d", "M120,164 C40,164 40,70 42,70")
           .attr("stroke", C.red).attr("stroke-width", 1.5)
           .attr("stroke-dasharray", "5 4").attr("fill", "none");
        svg.append("text").attr("x", 430).attr("y", 248).attr("text-anchor", "middle")
           .attr("fill", C.dim).style("font", "11px ui-monospace")
           .text("the flaw found in step 6 is what step 1 collects for next time");
    })();

    // ── Section rail ─────────────────────────────────────────────────────
    // Twelve sections and ~18,000px of page. Built from the DOM rather than a
    // hand-kept list so it cannot drift out of sync with the sections.
    (function rail() {
        var rail = document.getElementById("rail");
        if (!rail) return;
        var sections = [].slice.call(document.querySelectorAll("main > section[id]"));
        var links = sections.map(function (sec) {
            var h2 = sec.querySelector("h2");
            var num = h2 && h2.querySelector(".num");
            var a = document.createElement("a");
            a.href = "#" + sec.id;
            a.textContent = num ? num.textContent : (h2 ? h2.textContent.trim() : sec.id);
            a.title = h2 ? h2.textContent.replace(/^\s*\d+\s*/, "").trim() : sec.id;
            rail.appendChild(a);
            return a;
        });

        // Highlight whichever section owns the middle of the viewport. Using a
        // scroll handler rather than IntersectionObserver because sections here
        // are taller than the viewport, so "is intersecting" is true for
        // several at once and says nothing about which one you are reading.
        var ticking = false;
        function update() {
            ticking = false;
            var mid = window.scrollY + window.innerHeight * 0.35, best = 0;
            sections.forEach(function (sec, i) {
                if (sec.offsetTop <= mid) best = i;
            });
            links.forEach(function (a, i) { a.classList.toggle("active", i === best); });
            var on = links[best];
            if (on && on.offsetLeft < rail.scrollLeft ||
                on && on.offsetLeft + on.offsetWidth > rail.scrollLeft + rail.clientWidth) {
                rail.scrollTo({ left: on.offsetLeft - rail.clientWidth / 2, behavior: "smooth" });
            }
        }
        window.addEventListener("scroll", function () {
            if (!ticking) { ticking = true; requestAnimationFrame(update); }
        }, { passive: true });
        update();
    })();

    // ── Hero stats ───────────────────────────────────────────────────────
    // Read from the node this page is served by. A portfolio page describing a
    // live system loses its credibility the moment its numbers go stale, and
    // "these are read from the running machine" is itself part of the point.
    (function heroStats() {
        var el = document.getElementById("hero-stats");
        if (!el) return;
        var set = function (id, v) {
            var n = document.getElementById(id);
            if (n && v !== undefined && v !== null) n.textContent = v;
        };

        function load() {
            Promise.all([
                fetch("/api/iq-collect").then(function (r) { return r.json(); }),
                fetch("/api/classifier/status").then(function (r) { return r.json(); })
            ]).then(function (res) {
                var iq = res[0] || {}, clf = res[1] || {};
                var corpus = (iq.corpus && iq.corpus.total) || 0;
                set("hs-corpus", corpus.toLocaleString());
                // Deliberately NOT a hero stat: this counter is session-scoped
                // and resets with the service, so a restart would make the node
                // look like it had barely run. It belongs in the live line,
                // where "since restart" is obvious.
                var since = (clf.classifications_total || 0).toLocaleString();

                var val = (clf.validated_classes || []).length;
                var all = Object.keys(clf.validation || {}).length ||
                          ((clf.validated_classes || []).length +
                           (clf.unproven_classes || []).length);
                set("hs-validated", all ? val + " of " + all : "\u2014");

                var backend = { hailo: "Hailo NPU", onnx: "CPU / ONNX",
                                cpu: "heuristics", none: "\u2014" }[clf.backend] || clf.backend;
                set("hs-latency", backend);

                var live = document.getElementById("hero-live");
                if (live) {
                    live.innerHTML = '<span class="dot"></span>' +
                        (iq.capturing
                            ? "collecting now \u2014 " +
                              ((iq.current_band && iq.current_band.id) || "rotating bands")
                            : "idle between collection slots") +
                        " \u00b7 " + since + " signals classified since restart" +
                        " \u00b7 read live from this Pi";
                }
            }).catch(function () {
                var live = document.getElementById("hero-live");
                if (live) live.textContent = "console offline — figures unavailable";
            });
        }
        load();
        setInterval(load, 15000);
    })();

})();
