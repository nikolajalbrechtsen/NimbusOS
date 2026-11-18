// =============================================================================
// views/settings.js: the Settings page
// =============================================================================
// What: - Network sharing: SMB on or off, workgroup (default WORKGROUP),
//         server description, and NetBIOS (nmbd) for old Windows network
//         views, off by default; saved through the backend, which renders
//         Samba's settings and starts or stops smbd, wsdd2 and nmbd;
//       - Alerts: the disk temperature thresholds the dashboard warns at
//         (hard disks 50 °C and NVMe 70 °C by default);
//       - Firewall: NimbusOS has no active firewall; how to allow SMB
//         (port 445) with ufw or firewalld, with copy buttons.
// Why:  spec Tab C (Settings); docs/PLAN.md NOTE-22, NOTE-51, NOTE-68.
// =============================================================================

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const { h, clear } = ui;
  const body = h("div", { class: "stack" }, h("div", { class: "loading", text: "Loading settings…" }));
  root.append(
    h("div", { class: "page-head" }, h("div", {}, h("h1", { text: "Settings" }),
      h("p", { class: "desc", text: "Network sharing, alerts and the firewall." }))),
    body);

  let settings;
  try {
    settings = await api.get("/api/settings");
  } catch (error) {
    clear(body, ui.banner("bad", "The settings could not be read", ui.errorText(error)));
    return null;
  }
  const samba = { ...settings.samba };
  const alerts = { ...settings.alerts };

  async function save(part, values, panel) {
    ui.clearInvalid(panel);
    try {
      settings = await api.put("/api/settings", { [part]: values });
      ui.toast(part === "samba" ? "Network sharing saved." : "Alert thresholds saved.", "good");
      ctx.refreshSummary();
    } catch (error) {
      if (!ui.markInvalid(panel, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
    }
  }

  const sharing = ui.panel("Network sharing (SMB)", {}, h("div", { class: "stack" },
    h("div", { class: "field", "data-field": "samba.enabled" }, ui.radioCards({
      label: "SMB", value: samba.enabled ? "on" : "off",
      options: [{ value: "on", title: "On", desc: "Shares can be opened from Windows, macOS and Linux." },
        { value: "off", title: "Off", desc: "Samba is stopped; no computer can open the shares." }],
      onChange: (v) => { samba.enabled = v === "on"; return true; },
    })),
    h("div", { class: "grid-2" },
      ui.field("Workgroup", ui.input({ value: samba.workgroup, mono: true, label: "Workgroup", onInput: (v) => { samba.workgroup = v.trim(); } }),
        { key: "samba.workgroup", hint: "WORKGROUP unless your Windows computers use another." }),
      ui.field("Description", ui.input({ value: samba.description, label: "Description", onInput: (v) => { samba.description = v; } }),
        { key: "samba.description", hint: "Shown next to this computer in some network views." })),
    ui.checkbox("NetBIOS: show this computer in old Windows network views (starts nmbd)", samba.netbios, (v) => { samba.netbios = v; }),
    h("p", { class: "dim", text: "Windows 10 and 11 find this computer without NetBIOS (wsdd2), and Macs through Bonjour (Avahi)." }),
    h("div", { class: "inline" }, ui.button({ label: "Save", kind: "primary", onClick: () => save("samba", samba, sharing) }))));

  const alertPanel = ui.panel("Alerts", {}, h("div", { class: "stack" },
    h("p", { class: "muted", text: "The dashboard warns when a disk gets hotter than this." }),
    h("div", { class: "grid-2" },
      ui.field("Hard disks and SSDs (°C)", ui.input({ value: alerts.hdd_temp_c, type: "number", min: 30, max: 90, label: "Hard disk limit",
        onInput: (v) => { alerts.hdd_temp_c = Number(v); } }), { key: "alerts.hdd_temp_c" }),
      ui.field("NVMe drives (°C)", ui.input({ value: alerts.nvme_temp_c, type: "number", min: 40, max: 100, label: "NVMe limit",
        onInput: (v) => { alerts.nvme_temp_c = Number(v); } }), { key: "alerts.nvme_temp_c" })),
    h("div", { class: "inline" }, ui.button({ label: "Save", kind: "primary", onClick: () => save("alerts", alerts, alertPanel) }))));

  const ufw = "sudo ufw allow 445/tcp";
  const firewalld = "sudo firewall-cmd --permanent --add-service=samba && sudo firewall-cmd --reload";
  const firewall = ui.panel("Firewall", {}, h("div", { class: "stack" },
    h("p", { text: "NimbusOS ships without an active firewall. If you turn one on, allow SMB (TCP port 445) so other computers can still open the shares:" }),
    h("div", { class: "command" }, h("span", { class: "dim", text: "ufw" }), h("code", { class: "mono", text: ufw }), ui.copyButton(ufw, { label: "" })),
    h("div", { class: "command" }, h("span", { class: "dim", text: "firewalld" }), h("code", { class: "mono", text: firewalld }), ui.copyButton(firewalld, { label: "" }))));

  clear(body, sharing, alertPanel, firewall);
  return null;
}
