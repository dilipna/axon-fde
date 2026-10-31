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
