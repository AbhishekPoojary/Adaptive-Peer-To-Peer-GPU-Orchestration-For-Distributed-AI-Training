/**
 * The design system's accent, as a hex string.
 *
 * Some third-party components take a colour as a prop rather than reading a
 * CSS variable — Rare UI's `CodeBlock` builds its whole theme from one hex.
 * Passing a literal would put a second copy of the accent in the codebase,
 * free to drift from `--accent` the moment either changes. Reading the
 * computed value keeps `index.css` the only place the colour is decided.
 *
 * The fallback matters for the server-render and test paths, where there is no
 * document to compute against; it is the same value `index.css` declares.
 */
const FALLBACK = "#2d50e6";

export function accentHex(): string {
  if (typeof document === "undefined") return FALLBACK;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue("--accent")
    .trim();
  return value || FALLBACK;
}
