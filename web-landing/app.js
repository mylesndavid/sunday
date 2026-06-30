// Sunday landing — minimal vanilla JS, no dependencies.
(function () {
  "use strict";

  // Current year in footer.
  var yearEl = document.getElementById("year");
  if (yearEl) {
    yearEl.textContent = String(new Date().getFullYear());
  }

  // Add a border/background to the nav once the user scrolls.
  var nav = document.querySelector(".nav");
  if (nav) {
    var onScroll = function () {
      if (window.scrollY > 8) {
        nav.classList.add("scrolled");
      } else {
        nav.classList.remove("scrolled");
      }
    };
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
  }

  // Fade-in reveal on scroll via IntersectionObserver (graceful fallback).
  var revealEls = document.querySelectorAll(".reveal");

  if (!("IntersectionObserver" in window)) {
    revealEls.forEach(function (el) {
      el.classList.add("in");
    });
    return;
  }

  var observer = new IntersectionObserver(
    function (entries, obs) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in");
          obs.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.12, rootMargin: "0px 0px -8% 0px" }
  );

  // Stagger reveals within a shared parent for a gentler cascade.
  revealEls.forEach(function (el, i) {
    var siblings = el.parentElement
      ? el.parentElement.querySelectorAll(":scope > .reveal")
      : [];
    if (siblings.length > 1) {
      var idx = Array.prototype.indexOf.call(siblings, el);
      el.style.transitionDelay = Math.min(idx, 6) * 70 + "ms";
    }
    observer.observe(el);
  });
})();
