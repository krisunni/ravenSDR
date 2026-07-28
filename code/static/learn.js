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

})();
