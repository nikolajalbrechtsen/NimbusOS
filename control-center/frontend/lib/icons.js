// =============================================================================
// lib/icons.js: the Control Center's icons
// =============================================================================
// What: icon(name) returns an inline <svg class="icon"> drawn with 1.9 px
//       strokes on a 24 px grid (style.css sets stroke and size). Built with
//       DOM calls, so the CSP needs no exceptions.
// Why:  spec "Components": inline SVG icons, no icon font, no emoji.
// =============================================================================

const SVG = "http://www.w3.org/2000/svg";

// Each icon: a list of [element, attributes].
const ICONS = {
  dashboard: [["rect", { x: 3, y: 3, width: 7.5, height: 7.5, rx: 1.6 }], ["rect", { x: 13.5, y: 3, width: 7.5, height: 7.5, rx: 1.6 }],
    ["rect", { x: 3, y: 13.5, width: 7.5, height: 7.5, rx: 1.6 }], ["rect", { x: 13.5, y: 13.5, width: 7.5, height: 7.5, rx: 1.6 }]],
  apps: [["path", { d: "M21 7.7 12 3 3 7.7v8.6L12 21l9-4.7V7.7Z" }], ["path", { d: "m3 7.7 9 4.7 9-4.7" }], ["path", { d: "M12 12.4V21" }]],
  vms: [["rect", { x: 2.5, y: 4, width: 19, height: 13, rx: 2 }], ["path", { d: "M8 21h8M12 17v4" }]],
  storage: [["rect", { x: 3, y: 4, width: 18, height: 7, rx: 2 }], ["rect", { x: 3, y: 13, width: 18, height: 7, rx: 2 }],
    ["path", { d: "M7 7.5h.01M7 16.5h.01" }]],
  updates: [["path", { d: "M20.5 12a8.5 8.5 0 1 1-2.5-6" }], ["path", { d: "M20.5 3.5V8H16" }], ["path", { d: "M12 8v5.5M9.5 11l2.5 2.5 2.5-2.5" }]],
  settings: [["path", { d: "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1" }],
    ["circle", { cx: 15, cy: 6, r: 2 }], ["circle", { cx: 9, cy: 12, r: 2 }], ["circle", { cx: 17, cy: 18, r: 2 }]],
  search: [["circle", { cx: 11, cy: 11, r: 7 }], ["path", { d: "m20 20-3.6-3.6" }]],
  check: [["path", { d: "m5 12.5 4.5 4.5L19 7" }]],
  alert: [["path", { d: "M12 3.5 21.5 20h-19L12 3.5Z" }], ["path", { d: "M12 10v4M12 17.3h.01" }]],
  info: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 11v5.5M12 7.6h.01" }]],
  x: [["path", { d: "M6 6l12 12M18 6 6 18" }]],
  copy: [["rect", { x: 9, y: 9, width: 11, height: 11, rx: 2 }], ["path", { d: "M5 15V6a2 2 0 0 1 2-2h8" }]],
  play: [["path", { d: "M7.5 5 19 12 7.5 19V5Z" }]],
  stop: [["rect", { x: 6, y: 6, width: 12, height: 12, rx: 2 }]],
  restart: [["path", { d: "M3.5 12a8.5 8.5 0 1 0 2.5-6" }], ["path", { d: "M3.5 3.5V8H8" }]],
  external: [["path", { d: "M14 4h6v6M20 4l-9 9" }], ["path", { d: "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5" }]],
  power: [["path", { d: "M12 3v8" }], ["path", { d: "M6.4 6.6a8 8 0 1 0 11.2 0" }]],
  download: [["path", { d: "M12 4v11M7 10l5 5 5-5M5 20h14" }]],
  shield: [["path", { d: "M12 3 20 6v6c0 4.8-3.4 8-8 9-4.6-1-8-4.2-8-9V6l8-3Z" }], ["path", { d: "m9 12 2 2 4-4" }]],
  clock: [["circle", { cx: 12, cy: 12, r: 9 }], ["path", { d: "M12 7v5l3.2 2" }]],
  chevronDown: [["path", { d: "m6 9 6 6 6-6" }]],
  chevronUp: [["path", { d: "m6 15 6-6 6 6" }]],
  chevronRight: [["path", { d: "m9 6 6 6-6 6" }]],
  terminal: [["rect", { x: 3, y: 4, width: 18, height: 16, rx: 2 }], ["path", { d: "m7 9 3 3-3 3M13 15h4" }]],
  history: [["path", { d: "M3.5 12a8.5 8.5 0 1 0 2.6-6.1" }], ["path", { d: "M3.5 4v4.5H8" }], ["path", { d: "M12 8v4.5l3 1.8" }]],
};

export function icon(name, className = "icon") {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  for (const [tag, attrs] of ICONS[name] || ICONS.info) {
    const el = document.createElementNS(SVG, tag);
    for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
    svg.appendChild(el);
  }
  return svg;
}
