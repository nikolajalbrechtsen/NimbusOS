// =============================================================================
// app.js: the Control Center shell
// =============================================================================
// What: 1. signs in: trades the launcher's one-time ?launch= code for the
//          token (kept in sessionStorage for this window only) and removes
//          the code from the address bar; without either, explains how to
//          open the Control Center;
//       2. builds the shell: title bar (logo, name, host name and IP in mono,
//          search with Ctrl K), sidebar (Overview / Workloads / NAS / System,
//          an icon and a count per entry, the pending-updates badge) and its
//          footer (CPU, RAM and storage bars, uptime, version and channel),
//          refreshed every 5 seconds;
//       3. routes #dashboard #apps #vms #storage #updates #settings to
//          views/<name>.js, each exporting render(root, ctx) -> cleanup;
//          ctx.navigate(page, intent) can hand the page a one-time intent;
//       4. the search palette: sections, plus whatever the views register
//          (apps, VMs, shares).
// Why:  spec "App shell" and "Behavior".
// =============================================================================

import * as api from "./lib/api.js";
import { icon } from "./lib/icons.js";
import * as ui from "./lib/ui.js";

const { h, clear } = ui;

const ROUTES = [
  { id: "dashboard", title: "Dashboard", group: "Overview", icon: "dashboard", desc: "What is using this machine" },
  { id: "apps", title: "Apps", group: "Workloads", icon: "apps", count: "apps", desc: "Docker apps" },
  { id: "vms", title: "Virtual Machines", group: "Workloads", icon: "vms", count: "vms", desc: "KVM/QEMU virtual machines" },
  { id: "storage", title: "Storage & Shares", group: "NAS", icon: "storage", count: "shares", desc: "Disks, pool and network shares" },
  { id: "updates", title: "Updates", group: "System", icon: "updates", badge: "updates", desc: "NimbusOS and Debian updates" },
  { id: "settings", title: "Settings", group: "System", icon: "settings", desc: "Network sharing and alerts" },
];
const TOKEN_KEY = "nimbus.token";
const searchProviders = new Set();
let content;
let cleanup = null;
let summaryTimer = null;
const navItems = new Map();
const footer = {};

// --- sign-in ---------------------------------------------------------------------
async function signIn() {
  const params = new URLSearchParams(location.search);
  const code = params.get("launch");
  if (code) {
    history.replaceState(null, "", location.pathname + location.hash);
    try {
      const token = await api.exchangeLaunchCode(code);
      sessionStorage.setItem(TOKEN_KEY, token);
      return token;
    } catch (error) {
      showSignedOut(ui.errorText(error));
      return null;
    }
  }
  const stored = sessionStorage.getItem(TOKEN_KEY);
  if (!stored) showSignedOut("Open the Nimbus Control Center from the app menu.");
  return stored;
}

function showSignedOut(message) {
  sessionStorage.removeItem(TOKEN_KEY);
  if (summaryTimer) clearInterval(summaryTimer);
  clear(document.body, h("div", { class: "signed-out" },
    h("div", { class: "panel" },
      h("img", { src: "/logo.svg", alt: "" }),
      h("h1", { text: "Nimbus Control Center" }),
      h("p", { class: "muted", text: message }))));
}

// --- shell -------------------------------------------------------------------------
function buildShell() {
  const hostEl = h("span", { class: "host mono" });
  const searchBtn = h("button", { class: "search-button", type: "button", "aria-label": "Search apps, VMs and shares" },
    icon("search"), h("span", { text: "Search apps, VMs, shares…" }), h("kbd", { text: "Ctrl K" }));
  searchBtn.addEventListener("click", openSearch);

  const nav = h("nav", { class: "sidebar", "aria-label": "Sections" });
  const groups = new Map();
  for (const route of ROUTES) {
    if (!groups.has(route.group)) {
      const groupEl = h("div", { class: "nav-group" }, h("span", { class: "label", text: route.group }));
      groups.set(route.group, groupEl);
      nav.appendChild(groupEl);
    }
    const counter = h("span", { class: route.badge ? "badge" : "count", hidden: true });
    const item = h("a", { class: "nav-item", href: `#${route.id}` }, icon(route.icon), h("span", { text: route.title }), counter);
    navItems.set(route.id, { item, counter, route });
    groups.get(route.group).appendChild(item);
  }

  const mini = (label) => {
    const value = h("span", { class: "value num", text: "–" });
    const fill = h("i", { class: "fill" });
    const bar = h("div", { class: "bar thin" }, fill);
    footer[label] = { value, fill, bar };
    return h("div", { class: "mini" }, h("span", { text: label }), bar, value);
  };
  footer.uptime = h("span", { class: "num" });
  footer.version = h("span", { class: "mono" });
  nav.appendChild(h("div", { class: "sidebar-footer", "aria-label": "System usage" },
    mini("CPU"), mini("RAM"), mini("Disk"),
    h("div", { class: "meta" }, footer.uptime, footer.version)));

  content = h("main", { class: "content", id: "content", tabindex: "-1" });
  footer.host = hostEl;
  clear(document.body, h("div", { class: "shell" },
    h("header", { class: "titlebar" },
      h("img", { class: "logo", src: "/logo.svg", alt: "" }),
      h("span", { class: "app-name", text: "Nimbus Control Center" }),
      hostEl, searchBtn),
    nav, content));
}

function setMini(name, used, total, text) {
  const entry = footer[name];
  const share = total ? Math.min(1, used / total) : 0;
  entry.fill.style.width = `${(share * 100).toFixed(1)}%`;
  entry.bar.classList.toggle("warn", share >= 0.8 && share < 0.9);
  entry.bar.classList.toggle("bad", share >= 0.9);
  entry.value.textContent = text;
}

async function refreshSummary() {
  let s;
  try {
    s = await api.get("/api/system/summary");
  } catch {
    return;
  }
  setMini("CPU", s.cpu_percent, 100, ui.percent(s.cpu_percent));
  setMini("RAM", s.memory.used, s.memory.total, ui.percent((100 * s.memory.used) / s.memory.total));
  footer.Disk.bar.parentElement.firstChild.textContent = s.storage.label === "Pool" ? "Pool" : "Disk";
  setMini("Disk", s.storage.used, s.storage.total, ui.percent((100 * s.storage.used) / s.storage.total));
  footer.uptime.textContent = `Up ${ui.duration(s.uptime_s)}`;
  footer.version.textContent = `${s.version}${s.channel && s.channel !== "stable" ? ` · ${s.channel}` : ""}`;
  footer.version.title = `${s.name} ${s.version} (${s.channel} channel)`;
  for (const { counter, route } of navItems.values()) {
    const key = route.count || route.badge;
    const value = key ? s.counts[key] : null;
    const show = value !== null && value !== undefined && (route.count || value > 0);
    counter.hidden = !show;
    if (show) counter.textContent = String(value);
  }
}

async function loadInfo() {
  try {
    const info = await api.get("/api/system/info");
    footer.host.textContent = info.ip ? `${info.hostname} · ${info.ip}` : info.hostname;
    document.title = `Nimbus Control Center · ${info.hostname}`;
  } catch {
    /* the summary refresh shows connection problems */
  }
}

// --- routing -------------------------------------------------------------------------
async function route() {
  let id = location.hash.replace(/^#/, "") || "dashboard";
  if (!navItems.has(id)) id = "dashboard";
  for (const [key, { item }] of navItems) {
    if (key === id) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  if (cleanup) {
    try { cleanup(); } catch { /* the old view is gone either way */ }
    cleanup = null;
  }
  const routeInfo = navItems.get(id).route;
  clear(content, h("div", { class: "loading", text: "Loading…" }));
  const token = Symbol(id);
  route.current = token;
  try {
    const view = await import(`./views/${id}.js`);
    if (route.current !== token) return;
    clear(content);
    cleanup = (await view.render(content, context)) || null;
  } catch (error) {
    if (route.current !== token) return;
    clear(content, ui.emptyState({
      iconName: "alert", title: `${routeInfo.title} could not be shown`,
      text: `${ui.errorText(error)} Reload the window; if that doesn't help, update NimbusOS.`,
      action: ui.button({ label: "Reload", iconName: "restart", onClick: () => location.reload() }),
    }));
  }
  content.focus({ preventScroll: true });
}

// navigate(id, intent): show a page; INTENT (for example {fix: "jellyfin"}) is
// handed once to that page through takeIntent(id), so the Dashboard can open
// an app's Edit dialog or a Storage tab directly.
let pendingIntent = null;
const context = {
  api,
  ui,
  navigate: (id, intent = null) => {
    pendingIntent = intent ? { id, ...intent } : null;
    if (location.hash === `#${id}`) route();
    else location.hash = `#${id}`;
  },
  takeIntent: (id) => {
    if (!pendingIntent || pendingIntent.id !== id) return null;
    const intent = pendingIntent;
    pendingIntent = null;
    return intent;
  },
  refreshSummary,
  registerSearch(provider) { searchProviders.add(provider); return () => searchProviders.delete(provider); },
};

// --- search palette --------------------------------------------------------------------
async function openSearch() {
  if (document.querySelector(".palette")) return;
  const input = h("input", { type: "search", placeholder: "Search apps, VMs, shares and pages", "aria-label": "Search", autocomplete: "off" });
  const list = h("ul", { role: "listbox", "aria-label": "Results" });
  const dialog = h("div", { class: "modal palette", role: "dialog", "aria-modal": "true", "aria-label": "Search" },
    h("div", { class: "palette-input" }, icon("search"), input, h("kbd", { text: "Esc" })), list);
  const backdrop = h("div", { class: "backdrop palette-backdrop" }, dialog);
  let items = [];
  let active = 0;
  const previous = document.activeElement;
  function close() { backdrop.remove(); document.removeEventListener("keydown", onKey, true); previous && previous.focus && previous.focus(); }
  function pick(item) { close(); item.action(); }
  function draw() {
    clear(list);
    if (!items.length) { list.appendChild(h("li", { class: "none", text: "Nothing found." })); return; }
    items.forEach((item, i) => {
      const li = h("li", { role: "option", "aria-selected": String(i === active) }, icon(item.icon || "chevronRight"),
        h("span", { text: item.title }), item.sub ? h("span", { class: "sub", text: item.sub }) : null);
      li.addEventListener("mousedown", (event) => { event.preventDefault(); pick(item); });
      list.appendChild(li);
    });
  }
  async function search() {
    const q = input.value.trim().toLowerCase();
    const pages = ROUTES.filter((r) => !q || r.title.toLowerCase().includes(q) || r.desc.toLowerCase().includes(q))
      .map((r) => ({ title: r.title, sub: "Page", icon: r.icon, action: () => context.navigate(r.id) }));
    const extra = [];
    for (const provider of searchProviders) {
      try { extra.push(...(await provider(q))); } catch { /* one provider failing doesn't hide the others */ }
    }
    items = [...extra, ...pages].slice(0, 30);
    active = 0;
    draw();
  }
  function onKey(event) {
    if (event.key === "Escape") { event.preventDefault(); close(); }
    else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      active = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % Math.max(items.length, 1);
      draw();
    } else if (event.key === "Enter" && items[active]) { event.preventDefault(); pick(items[active]); }
  }
  backdrop.addEventListener("mousedown", (event) => { if (event.target === backdrop) close(); });
  input.addEventListener("input", search);
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(backdrop);
  input.focus();
  await search();
}

// --- start --------------------------------------------------------------------------------
async function main() {
  const token = await signIn();
  if (!token) return;
  api.setToken(token);
  api.onSignedOut(() => showSignedOut("Your session has ended (the computer was restarted, or the service was reinstalled). Open the Control Center again from the app menu."));
  buildShell();
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(); }
  });
  window.addEventListener("hashchange", route);
  await Promise.all([loadInfo(), refreshSummary()]);
  summaryTimer = setInterval(refreshSummary, 5000);
  await route();
}

main();
