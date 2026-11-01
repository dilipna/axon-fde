// Keep published proof/replay bookmarks useful after the overview was simplified.
const LEGACY_SECTIONS = new Set([
  "stats",
  "problem",
  "replay",
  "results",
  "engineering",
  "compare-section",
  "ai",
  "loop",
  "safety",
  "provenance",
]);
export function engineeringDestination(hash) {
  const anchor = String(hash).replace(/^#/, "");
  return LEGACY_SECTIONS.has(anchor) ? "engineering.html#" + anchor : null;
}
if (typeof window !== "undefined") {
  const route = () => {
    const target = engineeringDestination(window.location.hash);
    if (target)
      window.location.replace(new URL(target, window.location.href).href);
  };
  window.addEventListener("hashchange", route);
  route();
}

// Decorative motion only: recorded telemetry and the decision story are untouched.
if (typeof document !== "undefined") {
  const art = document.querySelector(".cp-delivery-art");
  const toggle = document.querySelector("#motion-toggle");
  if (art && toggle) {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    let requested = !preference.matches;
    const render = () => {
      const enabled = requested && !preference.matches;
      art.dataset.motion = enabled ? "running" : "paused";
      art.dataset.pageActive = String(!document.hidden);
      toggle.disabled = preference.matches;
      toggle.textContent = preference.matches
        ? "Motion reduced"
        : enabled
          ? "Pause illustration"
          : "Play illustration";
      toggle.title = preference.matches
        ? "Motion is off to respect your device's reduced-motion preference."
        : "Control decorative motion. Recorded readings do not change.";
    };
    toggle.hidden = false;
    toggle.addEventListener("click", () => {
      requested = !requested;
      render();
    });
    preference.addEventListener("change", () => {
      requested = !preference.matches;
      render();
    });
    document.addEventListener("visibilitychange", render);
    if ("IntersectionObserver" in window) {
      const observer = new IntersectionObserver(
        ([entry]) => {
          art.dataset.inView = String(entry.isIntersecting);
        },
        { threshold: 0.05 },
      );
      observer.observe(art);
    }
    render();
  }
}
