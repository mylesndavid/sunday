/* =========================================================
   Sunday — "a day with Sunday"
   One signature element: the sun. It arcs across the sky as you
   scroll — low and pre-dawn at the top, risen by mid-page, set by
   the bottom — while the background tints subtly through the day.
   Animate transform/opacity only. Restraint over flash.

   Libraries (CDN, no build): Lenis (smooth scroll), GSAP + ScrollTrigger.
   Everything degrades: reduced-motion or missing libs → static & legible.
   ========================================================= */
(function () {
  "use strict";

  var root = document.documentElement;
  var prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var hasGSAP = typeof window.gsap !== "undefined" && typeof window.ScrollTrigger !== "undefined";

  // Mark that JS is on so the CSS can take over reveal hiding.
  // We only do this when we can actually animate; otherwise leave content visible.
  if (hasGSAP && !prefersReduced) {
    root.classList.add("js");
  }

  /* ---------------------------------------------------------
     1. The sun. Single source of truth for its position.
        --x: horizontal offset from center (vw)
        --y: vertical position from top (vh)
        --scale: size multiplier (a touch bigger at the horizon)
        progress p ∈ [0,1] across the whole page.
     --------------------------------------------------------- */
  var sun = document.getElementById("sun");

  function placeSun(p) {
    if (!sun) return;
    // Arc: rises from low-left pre-dawn, peaks center-high midday, sets low-right.
    // y follows an inverted parabola (low → high → low). Keep it gentle.
    var arc = 1 - Math.pow(2 * p - 1, 2); // 0 at ends, 1 in the middle
    var y = 86 - arc * 64;                // 86vh (low) → 22vh (high) → 86vh
    var x = (p - 0.5) * 56;               // -28vw (left) → +28vw (right)
    var scale = 1 + (1 - arc) * 0.18;     // a little larger near the horizon
    sun.style.setProperty("--y", y.toFixed(2) + "vh");
    sun.style.setProperty("--x", x.toFixed(2) + "vw");
    sun.style.setProperty("--scale", scale.toFixed(3));
  }

  /* ---------------------------------------------------------
     2. Background day-phase tint. Warm tonal shifts only.
        We interpolate between the phase colors as we descend.
     --------------------------------------------------------- */
  var styles = getComputedStyle(root);
  function phaseVar(name) { return styles.getPropertyValue(name).trim(); }

  var stops = [
    hexRGB(phaseVar("--bg-dawn")    || "#14110d"),
    hexRGB(phaseVar("--bg-morning") || "#1b1611"),
    hexRGB(phaseVar("--bg-midday")  || "#221a12"),
    hexRGB(phaseVar("--bg-dusk")    || "#1a130f"),
    hexRGB(phaseVar("--bg-night")   || "#0e0b08")
  ];

  function tintBackground(p) {
    var seg = p * (stops.length - 1);
    var i = Math.min(Math.floor(seg), stops.length - 2);
    var t = seg - i;
    var a = stops[i], b = stops[i + 1];
    var r = Math.round(a[0] + (b[0] - a[0]) * t);
    var g = Math.round(a[1] + (b[1] - a[1]) * t);
    var bl = Math.round(a[2] + (b[2] - a[2]) * t);
    document.body.style.backgroundColor = "rgb(" + r + "," + g + "," + bl + ")";
  }

  function hexRGB(hex) {
    hex = hex.replace("#", "");
    if (hex.length === 3) hex = hex.replace(/(.)/g, "$1$1");
    return [
      parseInt(hex.slice(0, 2), 16),
      parseInt(hex.slice(2, 4), 16),
      parseInt(hex.slice(4, 6), 16)
    ];
  }

  /* ---------------------------------------------------------
     3. Reduced-motion / no-GSAP path: place everything statically.
        The sun sits low and warm; reveals are already visible.
     --------------------------------------------------------- */
  if (prefersReduced || !hasGSAP) {
    placeSun(0.18);          // low, pre-dawn-ish — calm and legible
    tintBackground(0.25);
    return;
  }

  /* ---------------------------------------------------------
     4. Smooth scroll (Lenis) wired into GSAP's ScrollTrigger.
     --------------------------------------------------------- */
  window.gsap.registerPlugin(window.ScrollTrigger);

  var lenis = null;
  if (typeof window.Lenis !== "undefined") {
    lenis = new window.Lenis({
      duration: 1.1,
      easing: function (t) { return Math.min(1, 1.001 - Math.pow(2, -10 * t)); },
      smoothWheel: true
    });
    lenis.on("scroll", window.ScrollTrigger.update);
    window.gsap.ticker.add(function (time) { lenis.raf(time * 1000); });
    window.gsap.ticker.lagSmoothing(0);
  }

  document.body.classList.add("is-ready");

  /* ---------------------------------------------------------
     5. The signature moment — drive the sun + tint across the
        full scroll of the document. scrub = buttery, frame-tied.
     --------------------------------------------------------- */
  placeSun(0);
  tintBackground(0);

  window.ScrollTrigger.create({
    trigger: document.body,
    start: "top top",
    end: "bottom bottom",
    scrub: true,
    onUpdate: function (self) {
      placeSun(self.progress);
      tintBackground(self.progress);
    }
  });

  /* ---------------------------------------------------------
     6. Reveals — quiet, intentional. Each .reveal eases in once,
        staggered within its section. Transform/opacity only.
     --------------------------------------------------------- */
  document.querySelectorAll("section").forEach(function (section) {
    var items = section.querySelectorAll(".reveal");
    if (!items.length) return;
    window.ScrollTrigger.batch(items, {
      start: "top 88%",
      once: true,
      onEnter: function (batch) {
        window.gsap.to(batch, {
          opacity: 1,
          y: 0,
          duration: 0.9,
          ease: "power3.out",
          stagger: 0.08,
          overwrite: true,
          onStart: function () {
            batch.forEach(function (el) { el.classList.add("is-in"); });
          }
        });
      }
    });
  });

  /* Anchor clicks (skip link, scroll cue) → smooth via Lenis when present. */
  document.querySelectorAll('a[href^="#"]').forEach(function (a) {
    a.addEventListener("click", function (e) {
      var id = a.getAttribute("href");
      if (id.length < 2) return;
      var target = document.querySelector(id);
      if (!target) return;
      e.preventDefault();
      if (lenis) lenis.scrollTo(target, { offset: 0 });
      else target.scrollIntoView({ behavior: "smooth" });
    });
  });

  // Keep triggers honest after fonts load / layout settles.
  window.addEventListener("load", function () { window.ScrollTrigger.refresh(); });
})();
