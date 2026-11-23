// =============================================================================
// views/dashboard.js: the Dashboard (the default page)
// =============================================================================
// What: "What is using my machine, and is anything wrong?"
//       - header: host name and a Live indicator, grey while the metrics
//         WebSocket is down; it reconnects with backoff (1, 2, 4 … 30 s);
//       - four cards: CPU (% , temperature, sparkline), Memory (used of total,
//         sparkline), Network (↓/↑ Mb/s, sparkline), Storage (pool or system
//         disk, bar, parity state);
//       - "Where your resources go": CPU and memory as stacked bars split into
//         System & desktop, Docker apps, Virtual machines and Free, with a
//         legend; "Needs attention" next to it: the alerts, worst first, each
//         with its fix (Fix opens the app's settings at the failing one), or
//         "All good";
//       - Docker apps and Virtual machines: one row each with status, CPU bar,
//         memory and one action (Open, Start, Fix; Console, Start);
//       - Quick open: running apps' web pages, VM consoles, shares (a click
//         copies \\host\share), and "+ Install an app".
//       Numbers come from GET /api/dashboard and WS /ws/metrics (every 2 s);
//       the app, VM and share lists from the same API as their pages, every
//       10 seconds and whenever the alerts change.
// Why:  spec Dashboard; docs/PLAN.md §4.4, NOTE-69.
// =============================================================================

import { sparkline, stackedBar, update } from "../lib/charts.js";
import { consoleModal } from "../lib/vnc.js";

const APP_STATES = {
  running: ["good", "Running"], starting: ["info", "Starting"], stopped: ["", "Stopped"],
  error: ["bad", "Error"], unknown: ["warn", "Unknown"],
};
const VM_STATES = {
  running: ["good", "Running"], paused: ["warn", "Paused"], stopping: ["info", "Shutting down"], stopped: ["", "Stopped"],
  crashed: ["bad", "Crashed"], suspended: ["warn", "Suspended"], unknown: ["warn", "Unknown"],
};
const HISTORY = 1800;

function ibytes(n, digits = 1) {
  if (n === null || n === undefined) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i === 0 ? 0 : digits)} ${units[i]}`;
}
const mbit = (bytesPerSecond) => {
  const v = (bytesPerSecond * 8) / 1e6;
  return `${v < 10 ? v.toFixed(1) : v.toFixed(0)} Mb/s`;
};
const pct = (v) => (v === null || v === undefined ? "–" : `${v < 10 ? v.toFixed(1) : v.toFixed(0)} %`);

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const { h, clear } = ui;
  let alive = true;
  let snap;
  let apps = { apps: [], docker: { ok: true } };
  let vms = { vms: [], libvirt: { ok: true } };
  let shares = { shares: [] };
  const history = { t: [], cpu: [], memory: [], net_rx: [], net_tx: [] };

  const live = ui.pill("", "Connecting…");
  const hostEl = h("span", { class: "mono" });
  const body = h("div", { class: "stack" }, h("div", { class: "loading", text: "Loading the dashboard…" }));
  root.append(h("div", { class: "page-head" },
    h("div", {}, h("h1", { text: "Dashboard" }), h("p", { class: "desc" }, "What is using ", hostEl, ", and whether anything needs you.")),
    h("div", { class: "actions" }, live)), body);

  try {
    [snap, apps, vms, shares] = await Promise.all([api.get("/api/dashboard"), api.get("/api/apps"), api.get("/api/vms"),
      api.get("/api/shares").catch(() => ({ shares: [] }))]);
  } catch (error) {
    clear(body, ui.banner("bad", "The dashboard could not be read", ui.errorText(error)));
    return null;
  }
  hostEl.textContent = snap.hostname;
  for (const key of Object.keys(history)) history[key] = (snap.history && snap.history[key]) || [];

  // --- the four cards -------------------------------------------------------------------
  function card(label, sparkOpts) {
    const value = h("div", { class: "value num" });
    const sub = h("div", { class: "sub" });
    const spark = sparkOpts ? sparkline([], { label }) : null;
    const extra = h("div", { class: "extra" });
    const el = h("section", { class: "panel stat-card" }, h("span", { class: "label", text: label }), value, sub, spark, extra);
    return { el, value, sub, spark, extra };
  }
  const cards = { cpu: card("CPU", true), memory: card("Memory", true), net: card("Network", true), storage: card("Storage", false) };

  function drawCards(latest) {
    const host = latest.host;
    cards.cpu.value.textContent = pct(host.cpu);
    cards.cpu.sub.textContent = [host.temperature !== null && host.temperature !== undefined ? `${Math.round(host.temperature)} °C` : null,
      `${host.threads} threads`, host.load ? `load ${host.load[0].toFixed(2)}` : null].filter(Boolean).join(" · ");
    update(cards.cpu.spark, history.cpu, { max: 100 });
    cards.memory.value.textContent = ibytes(host.memory.used);
    cards.memory.sub.textContent = `of ${ibytes(host.memory.total, 0)}${host.swap.total ? ` · swap ${ibytes(host.swap.used)}` : ""}`;
    update(cards.memory.spark, history.memory, { max: host.memory.total });
    cards.net.value.textContent = `↓ ${mbit(host.net.rx)}`;
    cards.net.sub.textContent = `↑ ${mbit(host.net.tx)}${host.net.iface ? ` · ${host.net.iface}` : ""}`;
    update(cards.net.spark, history.net_rx, { second: history.net_tx });
    const st = latest.storage;
    const disk = st.pool || st.system;
    cards.storage.value.textContent = disk ? `${ibytes(disk.used, 1)}` : "–";
    cards.storage.sub.textContent = disk ? `of ${ibytes(disk.total, 0)} · ${st.pool ? "pool" : "system disk"}` : "";
    const share = disk && disk.total ? disk.used / disk.total : 0;
    const bar = h("div", { class: `bar${share >= 0.9 ? " bad" : share >= 0.8 ? " warn" : ""}` },
      h("i", { class: "fill", style: { width: `${(100 * share).toFixed(1)}%` } }));
    const p = st.parity;
    const parity = !p ? h("span", { class: "dim", text: st.pool ? "No parity" : "" })
      : p.running ? ui.pill("info", "Parity syncing")
        : p.last_sync_ok === false ? ui.pill("bad", "Parity sync failed")
          : p.last_sync ? ui.pill("good", `Parity synced ${ui.timeAgo(p.last_sync)}`) : ui.pill("warn", "Parity never synced");
    clear(cards.storage.extra, bar, parity);
  }

  // --- where the resources go ---------------------------------------------------------------
  const resources = h("div", { class: "stack-sm" });
  function drawResources(latest) {
    const s = latest.split;
    const mem = s.memory;
    clear(resources,
      h("div", { class: "split-row" }, h("span", { class: "label", text: "CPU" }), stackedBar([
        { cls: "system", value: s.cpu.system, title: `System & desktop ${pct(s.cpu.system)}` },
        { cls: "docker", value: s.cpu.docker, title: `Docker apps ${pct(s.cpu.docker)}` },
        { cls: "vm", value: s.cpu.vms, title: `Virtual machines ${pct(s.cpu.vms)}` },
        { cls: "free", value: s.cpu.free, title: `Free ${pct(s.cpu.free)}` }], "CPU")),
      h("div", { class: "split-row" }, h("span", { class: "label", text: "Memory" }), stackedBar([
        { cls: "system", value: mem.system, title: `System & desktop ${ibytes(mem.system)}` },
        { cls: "docker", value: mem.docker, title: `Docker apps ${ibytes(mem.docker)}` },
        { cls: "vm", value: mem.vms, title: `Virtual machines ${ibytes(mem.vms)}` },
        { cls: "free", value: mem.free, title: `Free ${ibytes(mem.free)}` }], "Memory")),
      h("table", { class: "legend" }, h("thead", {}, h("tr", {}, h("th"), h("th", { text: "CPU" }), h("th", { text: "Memory" }))),
        h("tbody", {}, [["system", "System & desktop", s.cpu.system, mem.system], ["docker", "Docker apps", s.cpu.docker, mem.docker],
          ["vm", "Virtual machines", s.cpu.vms, mem.vms], ["free", "Free", s.cpu.free, mem.free]].map(([cls, name, c, m]) =>
          h("tr", {}, h("td", {}, h("i", { class: `swatch ${cls}` }), name), h("td", { class: "num", text: pct(c) }),
            h("td", { class: "num", text: ibytes(m) }))))));
  }

  // --- needs attention --------------------------------------------------------------------------
  const attention = h("div", {});
  let shownAlerts = "";
  async function runAction(a) {
    const act = a.action;
    try {
      if (act.kind === "fix-app") ctx.navigate("apps", { fix: act.target });
      else if (act.kind === "page") ctx.navigate(act.target);
      else if (act.kind === "storage") ctx.navigate("storage", { tab: act.target });
      else if (act.kind === "start-vm") ui.toast((await api.post(`/api/vms/${encodeURIComponent(act.target)}/actions/start`)).message, "good");
      else if (act.kind === "sync-parity") ui.toast((await api.post("/api/storage/parity/sync")).message, "good");
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }
  function drawAttention(list) {
    const key = JSON.stringify(list.map((a) => [a.id, a.title]));
    if (key === shownAlerts) return false;
    shownAlerts = key;
    if (!list.length) {
      clear(attention, h("div", { class: "all-good" }, ui.pill("good", "All good"),
        h("span", { class: "muted", text: "Apps, VMs, disks, parity, services and updates are fine." })));
      return true;
    }
    clear(attention, h("ul", { class: "alerts" }, list.map((a) => h("li", { class: `alert ${a.severity}` },
      h("div", { class: "text" }, h("strong", { text: a.title }), a.detail ? h("span", { class: "dim", text: a.detail }) : null),
      a.action && a.action.label ? ui.button({ label: a.action.label, small: true, kind: a.severity === "bad" ? "primary" : "",
        onClick: () => runAction(a) }) : null))));
    return true;
  }

  // --- apps and VMs -----------------------------------------------------------------------------
  const appRows = h("div", { class: "dash-rows" });
  const vmRows = h("div", { class: "dash-rows" });
  const usageRefs = { apps: {}, vms: {} };

  function usageCell(kind, key) {
    const fill = h("i", { class: kind === "vms" ? "vm" : "fill" });
    const cpu = h("span", { class: "num", text: "–" });
    const mem = h("span", { class: "num dim", text: "–" });
    usageRefs[kind][key] = { fill, cpu, mem };
    return h("div", { class: "dash-usage" }, h("div", { class: "bar thin" }, fill), cpu, mem);
  }

  function follow(job, title) {
    const drawer = ui.logDrawer(title);
    api.socket(`/ws/jobs/${job}`, { onMessage: (m) => {
      if (m.type === "log") drawer.log(m.line);
      else if (m.type === "done") { drawer.done(m.ok); ui.toast(m.message, m.ok ? "good" : "bad"); refreshLists(); }
    } });
  }

  function drawApps() {
    usageRefs.apps = {};
    if (!apps.docker.ok) {
      clear(appRows, ui.banner("bad", "Docker is not available", apps.docker.message));
      return;
    }
    if (!apps.apps.length) {
      clear(appRows, h("div", { class: "dash-empty" }, h("span", { class: "muted", text: "No apps installed yet." }),
        ui.button({ label: "+ Install an app", small: true, onClick: () => ctx.navigate("apps", { tab: "catalog" }) })));
      return;
    }
    clear(appRows, apps.apps.map((a) => {
      const [kind, label] = a.busy ? ["info", "Working…"] : (APP_STATES[a.state] || APP_STATES.unknown);
      const status = ui.pill(kind, label);
      if (a.reason) status.title = a.reason;
      let action = null;
      if (a.busy) action = null;
      else if (a.state === "error") action = ui.button({ label: "Fix", small: true, kind: "primary", onClick: () => ctx.navigate("apps", { fix: a.name }) });
      else if (["running", "starting"].includes(a.state) && a.webui) {
        action = ui.button({ label: "Open", small: true, iconName: "external", onClick: () => window.open(a.webui, "_blank", "noopener") });
      } else if (a.state === "stopped") {
        action = ui.button({ label: "Start", small: true, iconName: "play", onClick: async () => {
          try { follow((await api.post(`/api/apps/${encodeURIComponent(a.name)}/actions/start`)).job, `Starting ${a.name}`); } catch (error) { ui.toast(ui.errorText(error), "bad"); }
        } });
      }
      return h("div", { class: "dash-row" },
        a.icon ? h("img", { class: "app-icon", src: a.icon, alt: "" }) : h("span", { class: "app-icon letter", text: (a.app_name || a.name).charAt(0) }),
        h("div", { class: "names" }, h("strong", { text: a.name }), h("span", { class: "dim", text: a.app_name })),
        status, usageCell("apps", a.name), h("div", { class: "act" }, action));
    }));
  }

  function drawVms() {
    usageRefs.vms = {};
    if (!vms.libvirt.ok) {
      clear(vmRows, ui.banner("bad", "libvirt is not available", vms.libvirt.message));
      return;
    }
    if (!vms.vms.length) {
      clear(vmRows, h("div", { class: "dash-empty" }, h("span", { class: "muted", text: "No virtual machines yet." }),
        ui.button({ label: "Create a VM", small: true, onClick: () => ctx.navigate("vms") })));
      return;
    }
    clear(vmRows, vms.vms.map((vm) => {
      const [kind, label] = VM_STATES[vm.state] || VM_STATES.unknown;
      const status = ui.pill(kind, label);
      if (vm.reason) status.title = vm.reason;
      const action = vm.console ? ui.button({ label: "Console", small: true, iconName: "vms", onClick: () => consoleModal(api, ui, vm) })
        : ["stopped", "crashed"].includes(vm.state) ? ui.button({ label: "Start", small: true, iconName: "play", onClick: async () => {
          try { ui.toast((await api.post(`/api/vms/${encodeURIComponent(vm.name)}/actions/start`)).message, "good"); await refreshLists(); } catch (error) { ui.toast(ui.errorText(error), "bad"); }
        } }) : null;
      return h("div", { class: "dash-row" },
        h("span", { class: `app-icon letter vm-${vm.preset || "other"}`, text: (vm.preset === "windows" ? "W" : vm.preset === "linux" ? "L" : vm.name.charAt(0)) }),
        h("div", { class: "names" }, h("strong", { text: vm.name }), h("span", { class: "dim", text: `${vm.vcpus} vCPU · ${ibytes(vm.memory, 0)}` })),
        status, usageCell("vms", vm.uuid), h("div", { class: "act" }, action));
    }));
  }

  function drawUsage(latest) {
    for (const [name, ref] of Object.entries(usageRefs.apps)) {
      const u = latest.apps[name];
      ref.fill.style.width = `${u ? Math.min(100, u.cpu) : 0}%`;
      ref.cpu.textContent = u ? pct(u.cpu) : "–";
      ref.mem.textContent = u ? ibytes(u.memory) : "–";
    }
    for (const [uid, ref] of Object.entries(usageRefs.vms)) {
      const u = latest.vms[uid];
      ref.fill.style.width = `${u && u.cpu !== null ? Math.min(100, u.cpu) : 0}%`;
      ref.cpu.textContent = u && u.cpu !== null ? pct(u.cpu) : "–";
      ref.mem.textContent = u ? ibytes(u.memory) : "–";
    }
  }

  // --- quick open ---------------------------------------------------------------------------------
  const quick = h("div", { class: "stack" });
  function tile({ icon = null, letter = null, title, sub, onClick, label }) {
    return h("button", { type: "button", class: "tile", title: label || title, onclick: onClick },
      icon ? h("img", { class: "app-icon", src: icon, alt: "" }) : h("span", { class: "app-icon letter", text: (letter || title.charAt(0)).toUpperCase() }),
      h("span", { class: "names" }, h("strong", { text: title }), h("span", { class: "dim mono", text: sub })));
  }
  function drawQuick() {
    const open = apps.apps.filter((a) => a.webui && ["running", "starting"].includes(a.state));
    const consoles = vms.vms.filter((vm) => vm.console);
    const groups = [
      ["Apps", open.map((a) => tile({ icon: a.icon, letter: (a.app_name || a.name).charAt(0), title: a.name,
        sub: a.webui.replace(/^https?:\/\//, ""), label: `Open ${a.name} (${a.webui})`, onClick: () => window.open(a.webui, "_blank", "noopener") }))],
      ["VM consoles", consoles.map((vm) => tile({ letter: vm.name.charAt(0), title: vm.name, sub: "Console",
        onClick: () => consoleModal(api, ui, vm) }))],
      ["Shares", shares.shares.filter((s) => s.smb).map((s) => tile({ letter: s.name.charAt(0), title: s.name, sub: s.connect.windows,
        label: `Copy ${s.connect.windows}`, onClick: async () => {
          try { await navigator.clipboard.writeText(s.connect.windows); ui.toast(`Copied ${s.connect.windows}`, "good"); } catch { ui.toast(s.connect.windows, "info"); }
        } }))],
    ];
    clear(quick, ...groups.filter(([, tiles]) => tiles.length).map(([name, tiles]) =>
      h("div", { class: "tile-group" }, h("span", { class: "label", text: name }), h("div", { class: "tiles" }, tiles))),
    h("div", { class: "tiles" }, h("button", { type: "button", class: "tile add", onclick: () => ctx.navigate("apps", { tab: "catalog" }) },
      h("span", { class: "app-icon letter", text: "+" }), h("span", { class: "names" }, h("strong", { text: "Install an app" }),
        h("span", { class: "dim", text: "From the catalog" })))));
  }

  // --- assembling ---------------------------------------------------------------------------------
  clear(body,
    h("div", { class: "dash-cards" }, cards.cpu.el, cards.memory.el, cards.net.el, cards.storage.el),
    h("div", { class: "grid-2" }, ui.panel("Where your resources go", {}, resources), ui.panel("Needs attention", {}, attention)),
    h("div", { class: "grid-2" },
      ui.panel("Docker apps", { actions: [ui.button({ label: "All apps", small: true, kind: "ghost", onClick: () => ctx.navigate("apps") })] }, appRows),
      ui.panel("Virtual machines", { actions: [ui.button({ label: "All VMs", small: true, kind: "ghost", onClick: () => ctx.navigate("vms") })] }, vmRows)),
    ui.panel("Quick open", {}, quick));

  function apply(latest) {
    drawCards(latest);
    drawResources(latest);
    drawUsage(latest);
    return drawAttention(latest.alerts || []);
  }
  drawApps();
  drawVms();
  drawQuick();
  if (snap.host) apply(snap);

  async function refreshLists() {
    try {
      [apps, vms] = await Promise.all([api.get("/api/apps"), api.get("/api/vms")]);
      if (!alive) return;
      drawApps();
      drawVms();
      drawQuick();
      if (snap.host) drawUsage(snap);
    } catch { /* keep the last lists */ }
  }
  const listTimer = setInterval(refreshLists, 10000);

  // --- live updates ---------------------------------------------------------------------------------
  let ws = null;
  let retry = 1;
  let retryTimer = null;
  function setLive(on) {
    live.className = on ? "pill good" : "pill";
    live.textContent = on ? "Live" : "Reconnecting…";
  }
  function connect() {
    if (!alive) return;
    ws = api.socket("/ws/metrics", {
      onOpen: () => { retry = 1; setLive(true); },
      onMessage: (m) => {
        if (m.type !== "tick") return;
        snap = m;
        for (const [key, value] of [["t", m.t], ["cpu", m.host.cpu], ["memory", m.host.memory.used], ["net_rx", m.host.net.rx], ["net_tx", m.host.net.tx]]) {
          history[key].push(value);
          if (history[key].length > HISTORY) history[key].shift();
        }
        if (apply(m)) refreshLists();              // something changed: the rows too
      },
      onClose: () => {
        setLive(false);
        if (!alive) return;
        retryTimer = setTimeout(async () => {
          try {
            const fresh = await api.get("/api/dashboard");
            for (const key of Object.keys(history)) history[key] = fresh.history[key] || [];
            snap = fresh;
            apply(fresh);
          } catch { /* the socket retry says the rest */ }
          connect();
        }, retry * 1000);
        retry = Math.min(30, retry * 2);
      },
    });
  }
  connect();

  return () => {
    alive = false;
    clearInterval(listTimer);
    clearTimeout(retryTimer);
    if (ws) ws.close();
  };
}
