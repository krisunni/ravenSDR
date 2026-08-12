// ravenSDR "the codebase" explainer.
//
// This page is prose and tables, not signal animations, so it needs none of
// learn.js's canvas machinery or D3 — loading them here would cost a vendored
// 280 KB to render nothing. The only shared behaviour is the section rail, and
// it is short enough that copying it beats importing learn.js and having its
// twenty animation blocks all no-op against absent element IDs.

(function () {
    "use strict";

    // Built from the DOM rather than a hand-kept list, so adding a section to
    // the template cannot leave the rail out of sync with the page.
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

    // Highlight whichever section owns the upper third of the viewport. A
    // scroll handler rather than IntersectionObserver because sections here are
    // taller than the viewport, so "is intersecting" is true for several at
    // once and says nothing about which one you are reading.
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
