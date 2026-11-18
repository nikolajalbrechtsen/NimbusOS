// =============================================================================
// lib/ui.js: the Control Center's components
// =============================================================================
// What: h() builds elements (text always through textContent, never HTML);
//       the components of the design system on top of it:
//         button() with a busy state, pill(), tag(), banner(), emptyState(),
//         radioCards() (role=radiogroup, arrow keys), segmented() (role=tablist),
//         copyButton() (Clipboard API, select-text fallback), panel(),
//         toast(), modal() (Esc and backdrop close, sticky head and foot,
//         a hint on the left of the foot), confirmDialog() (in-page, never
//         confirm(); optional "type X to confirm"), logDrawer();
//       and formatters: bytes, percent, duration, timeAgo, dateTime.
// Why:  spec "UI design system": components, behaviour, accessibility.
// =============================================================================

import { icon } from "./icons.js";

// --- elements ---------------------------------------------------------------
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "style") Object.assign(el.style, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2).toLowerCase(), value);
    else el.setAttribute(key, value === true ? "" : String(value));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

export function clear(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

// --- buttons --------------------------------------------------------------------
// button({label, kind: "primary"|"ghost"|"danger", small, icon, title, onClick})
// onClick may return a promise; the button shows its busy state until it settles.
export function button({ label = "", kind = "", small = false, iconName = null, title = null, onClick = null, disabled = false, type = "button" } = {}) {
  const cls = ["btn", kind, small ? "sm" : "", !label && iconName ? "icon-only" : ""].filter(Boolean).join(" ");
  const btn = h("button", { class: cls, type, title, "aria-label": !label ? title : null, disabled },
    iconName ? icon(iconName) : null, label ? h("span", { text: label }) : null);
  if (onClick) btn.addEventListener("click", (event) => busy(btn, () => onClick(event)));
  return btn;
}

export async function busy(btn, fn) {
  if (btn.classList.contains("is-busy")) return undefined;
  btn.classList.add("is-busy");
  btn.setAttribute("aria-busy", "true");
  try {
    return await fn();
  } finally {
    btn.classList.remove("is-busy");
    btn.removeAttribute("aria-busy");
  }
}

// --- small things -------------------------------------------------------------------
export const pill = (kind, text) => h("span", { class: `pill ${kind || ""}`.trim(), text });
export const tag = (kind, text) => h("span", { class: `tag ${kind || ""}`.trim(), text });

export function banner(kind, title, text, ...actions) {
  const icons = { warn: "alert", bad: "alert", info: "info", good: "check" };
  return h("div", { class: `banner ${kind}`, role: kind === "bad" ? "alert" : "status" },
    icon(icons[kind] || "info"),
    h("div", { class: "text" }, h("strong", { text: title }), text ? h("span", { text }) : null),
    ...actions);
}

export function emptyState({ iconName = "info", title, text, action = null }) {
  return h("div", { class: "empty" }, icon(iconName), h("h3", { text: title }), text ? h("p", { text }) : null, action);
}

export function panel(title, { actions = [], flush = false } = {}, ...body) {
  return h("section", { class: "panel" },
    title ? h("div", { class: "panel-head" }, h("h2", { text: title }), actions.length ? h("div", { class: "actions" }, ...actions) : null) : null,
    h("div", { class: flush ? "panel-body flush" : "panel-body" }, ...body));
}

// --- choices --------------------------------------------------------------------------
// radioCards({label, options: [{value, title, desc, disabled}], value, onChange})
// onChange may return a promise or false (to keep the old value).
export function radioCards({ label, options, value, onChange }) {
  const group = h("div", { class: "radio-cards", role: "radiogroup", "aria-label": label });
  let current = value;
  const cards = options.map((opt) => {
    const card = h("button", {
      type: "button", class: "radio-card", role: "radio", "aria-checked": String(opt.value === current),
      "aria-disabled": opt.disabled ? "true" : null, tabindex: opt.value === current ? "0" : "-1",
    }, h("span", { class: "rc-title", text: opt.title }), opt.desc ? h("span", { class: "rc-desc", text: opt.desc }) : null);
    card.addEventListener("click", () => choose(opt));
    return card;
  });
  async function choose(opt) {
    if (opt.disabled || opt.value === current) return;
    const ok = await busy(group, async () => (onChange ? onChange(opt.value) : true));
    if (ok === false) return;
    current = opt.value;
    cards.forEach((card, i) => {
      const on = options[i].value === current;
      card.setAttribute("aria-checked", String(on));
      card.tabIndex = on ? 0 : -1;
    });
  }
  group.addEventListener("keydown", (event) => {
    const i = cards.indexOf(document.activeElement);
    if (i < 0 || !["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    const step = event.key === "ArrowRight" || event.key === "ArrowDown" ? 1 : -1;
    const next = cards[(i + step + cards.length) % cards.length];
    next.focus();
  });
  group.append(...cards);
  return group;
}

export function segmented({ label, options, value, onChange }) {
  const bar = h("div", { class: "seg", role: "tablist", "aria-label": label });
  const buttons = options.map((opt) => {
    const b = h("button", { type: "button", role: "tab", "aria-selected": String(opt.value === value), text: opt.title });
    b.addEventListener("click", () => {
      buttons.forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      onChange && onChange(opt.value);
    });
    return b;
  });
  bar.append(...buttons);
  return bar;
}

export function copyButton(text, { label = "Copy", toastText = "Copied." } = {}) {
  return button({
    label, small: true, iconName: "copy", kind: "ghost", title: `Copy ${text}`,
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast(toastText, "good");
      } catch {
        const field = h("input", { class: "input mono", value: text, readonly: true });
        const m = modal({ title: "Copy", body: [h("p", { class: "muted", text: "Press Ctrl+C to copy:" }), field] });
        field.focus();
        field.select();
        return m;
      }
      return undefined;
    },
  });
}

// --- toasts -----------------------------------------------------------------------------
let toastHost = null;
export function toast(message, kind = "good", ms = 4200) {
  if (!toastHost) {
    toastHost = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    document.body.appendChild(toastHost);
  }
  const icons = { good: "check", bad: "alert", warn: "alert", info: "info" };
  const el = h("div", { class: `toast ${kind}` }, icon(icons[kind] || "info"), h("span", { text: message }));
  toastHost.appendChild(el);
  setTimeout(() => el.remove(), kind === "bad" ? ms * 2 : ms);
  return el;
}

// --- modals ------------------------------------------------------------------------------
// modal({title, body: Node|[Node], actions: [Node], hint, wide, onClose}) -> {el, close, setHint}
export function modal({ title, body = [], actions = [], hint = "", wide = false, onClose = null }) {
  const previous = document.activeElement;
  const hintEl = h("span", { class: "hint", text: hint });
  const titleId = `modal-${Math.random().toString(36).slice(2)}`;
  const dialog = h("div", { class: wide ? "modal wide" : "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("div", { class: "modal-head" }, h("h2", { id: titleId, text: title }),
      button({ iconName: "x", kind: "ghost", small: true, title: "Close", onClick: () => close() })),
    h("div", { class: "modal-body" }, ...[body].flat()),
    actions.length || hint ? h("div", { class: "modal-foot" }, hintEl, ...actions) : null);
  const backdrop = h("div", { class: "backdrop" }, dialog);
  function onKey(event) {
    if (event.key === "Escape") { event.stopPropagation(); close(); }
    if (event.key === "Tab") trapFocus(dialog, event);
  }
  backdrop.addEventListener("mousedown", (event) => { if (event.target === backdrop) close(); });
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(backdrop);
  const first = dialog.querySelector(".modal-body input, .modal-body select, .modal-body textarea, .modal-foot .btn.primary, .modal-foot .btn");
  (first || dialog).focus();
  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
    if (previous && previous.focus) previous.focus();
    onClose && onClose();
  }
  return { el: dialog, close, setHint: (text) => { hintEl.textContent = text; } };
}

function trapFocus(container, event) {
  const items = [...container.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
    .filter((el) => !el.disabled && el.offsetParent !== null);
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

// confirmDialog({title, message, confirmLabel, danger, typeToConfirm}) -> Promise<boolean>
export function confirmDialog({ title, message, confirmLabel = "Continue", danger = false, typeToConfirm = null, details = null }) {
  return new Promise((resolve) => {
    let answered = false;
    const field = typeToConfirm ? h("input", { class: "input mono", autocomplete: "off", spellcheck: "false", "aria-label": `Type ${typeToConfirm} to confirm` }) : null;
    const ok = button({ label: confirmLabel, kind: danger ? "danger solid" : "primary", disabled: Boolean(typeToConfirm),
      onClick: () => { answered = true; m.close(); resolve(true); } });
    if (field) field.addEventListener("input", () => { ok.disabled = field.value.trim() !== typeToConfirm; });
    const m = modal({
      title,
      body: [h("p", { text: message }), details, typeToConfirm ? h("label", { class: "field" },
        h("span", {}, "Type ", h("strong", { class: "mono", text: typeToConfirm }), " to confirm"), field) : null],
      actions: [button({ label: "Cancel", onClick: () => m.close() }), ok],
      onClose: () => { if (!answered) resolve(false); },
    });
  });
}

// --- log drawer ------------------------------------------------------------------------------
// logDrawer(title) -> {log(line), status(kind, text), progress(fraction|null), done(ok, text), close()}
let openDrawer = null;
export function logDrawer(title) {
  if (openDrawer) openDrawer.close();
  const statusPill = pill("info", "Running");
  const bar = h("div", { class: "progress indeterminate", role: "progressbar", "aria-label": title }, h("i"));
  const logEl = h("pre", { class: "log", tabindex: "0", "aria-live": "off" });
  const toggle = button({ iconName: "chevronDown", kind: "ghost", small: true, title: "Collapse" });
  const closeBtn = button({ iconName: "x", kind: "ghost", small: true, title: "Close" });
  const el = h("aside", { class: "drawer", role: "region", "aria-label": title },
    h("div", { class: "drawer-head" }, h("h2", { text: title }), statusPill, toggle, closeBtn), bar, logEl);
  document.body.appendChild(el);
  let stick = true;
  logEl.addEventListener("scroll", () => { stick = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 8; });
  toggle.addEventListener("click", () => {
    const collapsed = el.classList.toggle("collapsed");
    clear(toggle, icon(collapsed ? "chevronUp" : "chevronDown"));
  });
  const api = {
    el,
    log(line) {
      logEl.appendChild(document.createTextNode(`${line}\n`));
      if (stick) logEl.scrollTop = logEl.scrollHeight;
    },
    status(kind, text) { statusPill.className = `pill ${kind}`; statusPill.textContent = text; },
    progress(fraction) {
      bar.classList.toggle("indeterminate", fraction === null);
      bar.firstChild.style.width = fraction === null ? "" : `${Math.round(fraction * 100)}%`;
    },
    done(ok, text) {
      bar.classList.remove("indeterminate");
      bar.classList.add(ok ? "good" : "bad");
      api.status(ok ? "good" : "bad", text || (ok ? "Done" : "Failed"));
    },
    close() { el.remove(); if (openDrawer === api) openDrawer = null; },
  };
  closeBtn.addEventListener("click", () => api.close());
  openDrawer = api;
  return api;
}

// --- formatting ---------------------------------------------------------------------------------
export function bytes(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return "–";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0;
  let v = Number(n);
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i += 1; }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}

export const percent = (v) => (v === null || v === undefined ? "–" : `${Math.round(v)} %`);

export function duration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0));
  const d = Math.floor(s / 86400);
  const hrs = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d} d ${hrs} h`;
  if (hrs) return `${hrs} h ${m} min`;
  return `${m} min`;
}

export function timeAgo(iso) {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

export function dateTime(iso) {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso || "";
  // A plain date ("2026-11-17") has no time of day worth showing.
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return new Date(t).toLocaleDateString(undefined, { dateStyle: "medium", timeZone: "UTC" });
  return new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function errorText(error) {
  return (error && error.message) || "Something went wrong.";
}
