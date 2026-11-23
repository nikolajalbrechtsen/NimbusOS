// =============================================================================
// lib/charts.js: sparklines and stacked bars, with no chart library
// =============================================================================
// What: sparkline(values, {max, second, label}) draws inline SVG: an area
//       fill under a 1.6 px line (style.css: svg.spark), scaled to the
//       largest value or MAX, stretched to its box; SECOND draws a second line
//       (network: download area, upload line). update(el, ...) redraws it in
//       place. stackedBar(parts, label) is one bar of coloured segments
//       ({cls: system|docker|vm|free, value, title}) with role=img and a
//       text alternative.
// Why:  spec Dashboard (inline SVG sparklines, stacked bars in the same
//       colours everywhere: grey system, accent Docker, violet VMs).
// =============================================================================

const SVG = "http://www.w3.org/2000/svg";
const W = 100;
const H = 40;

function el(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

function points(values, max) {
  const n = values.length;
  if (!n) return [];
  const top = Math.max(max || 0, ...values, 1e-9);
  return values.map((v, i) => [n === 1 ? W : (i * W) / (n - 1), H - (Math.max(0, v) / top) * (H - 2) - 1]);
}

const line = (pts) => pts.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(2)},${y.toFixed(2)}`).join("");

export function update(svg, values, { max = null, second = null } = {}) {
  const top = max || Math.max(...values, ...(second || []), 0) || 1;
  const pts = points(values, top);
  const [area, stroke, stroke2] = svg.children;
  stroke.setAttribute("d", line(pts));
  area.setAttribute("d", pts.length ? `${line(pts)}L${W},${H}L0,${H}Z` : "");
  stroke2.setAttribute("d", second ? line(points(second, top)) : "");
  return svg;
}

export function sparkline(values, { max = null, second = null, label = "" } = {}) {
  const svg = el("svg", { class: "spark", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
    "aria-label": label });
  svg.append(el("path", { class: "area" }), el("path", { class: "line" }), el("path", { class: "line second" }));
  return update(svg, values, { max, second });
}

export function stackedBar(parts, label) {
  const total = parts.reduce((s, p) => s + Math.max(0, p.value), 0) || 1;
  const bar = document.createElement("div");
  bar.className = "bar stacked";
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", `${label}: ${parts.map((p) => p.title).join(", ")}`);
  for (const p of parts) {
    const seg = document.createElement("i");
    seg.className = p.cls;
    seg.style.width = `${(100 * Math.max(0, p.value)) / total}%`;
    seg.title = p.title;
    bar.appendChild(seg);
  }
  return bar;
}
