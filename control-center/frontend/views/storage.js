// =============================================================================
// views/storage.js: the Storage & Shares page
// =============================================================================
// What: four tabs:
//       - Shares: each share with its security, recycle bin and Time Machine
//         tags, its folder, usage and free space, and copyable connection
//         strings (\\host\share, smb://host.local/share); New, Edit and
//         Delete (its files only after a second, typed confirmation);
//       - Disks: model, serial, size, temperature, SMART health, filesystem,
//         mount point and role (System, Data, Parity, Unassigned); Format
//         (the serial typed to confirm), Add an existing filesystem,
//         Unmount, refresh SMART. The system disk and the live medium can't
//         be chosen;
//       - Pool: Single disk, Pooled (mergerfs) or Pooled + parity (SnapRAID),
//         its disks and create policy; the parity card (last sync, scrub,
//         changes not yet protected, Sync now) says plainly that parity is
//         scheduled, not real time;
//       - Users: SMB users, the shares each can reach, set password, delete,
//         Test connection.
//       Without shares, "Set up NAS" opens the wizard: disks → mode → shares
//       → first user → confirm (serials typed) → done, with the connection
//       strings and Test connection. In the live session a banner says to
//       install NimbusOS first, and nothing can be changed.
// Why:  spec Tab C; docs/PLAN.md NOTE-26, NOTE-68.
// =============================================================================

const SECURITY = {
  public: ["warn", "Public", "Anyone on the network can read and write, without a password."],
  secure: ["accent", "Secure", "Everyone can read; the users you choose can also write."],
  private: ["good", "Private", "Only the users you choose, each with their own access."],
};
const ROLES = { system: ["", "System"], data: ["accent", "Data"], parity: ["violet", "Parity"], unassigned: ["warn", "Unassigned"] };
const ACCESS = [{ value: "rw", label: "Read/Write" }, { value: "ro", label: "Read-only" }, { value: "none", label: "No access" }];
const WIZARD_SHARES = {
  media: "Secure: everyone can play your media; you add to it.",
  documents: "Private: your documents.",
  backups: "Private: backups of your computers.",
  isos: "Secure: installer ISOs, also used by the VM tab.",
  timemachine: "Private: a Time Machine backup target for Macs.",
};

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const { h, clear } = ui;
  let tab = "shares";
  let st = null;          // GET /api/storage
  let sh = null;          // GET /api/shares
  let settings = null;    // GET /api/settings
  let alive = true;

  const body = h("div", { class: "stack" }, h("div", { class: "loading", text: "Loading storage…" }));
  const tabs = ui.segmented({
    label: "Storage", value: tab,
    options: [{ value: "shares", title: "Shares" }, { value: "disks", title: "Disks" }, { value: "pool", title: "Pool" },
      { value: "users", title: "Users" }],
    onChange: (v) => { tab = v; draw(); },
  });
  const headActions = h("div", { class: "actions" });
  root.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: "Storage & Shares" }),
        h("p", { class: "desc", text: "Disks, the pool and network folders for Windows, macOS and Linux." })),
      headActions),
    h("div", { style: { marginBottom: "16px" } }, tabs),
    body);

  // --- data -------------------------------------------------------------------------------
  async function load() {
    try {
      [st, sh, settings] = await Promise.all([api.get("/api/storage"), api.get("/api/shares"), api.get("/api/settings")]);
      if (alive) draw();
    } catch (error) {
      clear(body, ui.banner("bad", "Storage could not be read", ui.errorText(error)));
    }
  }
  const timer = setInterval(async () => {
    if (!alive || document.querySelector(".backdrop")) return;
    try {
      sh = await api.get("/api/shares");                 // usage arrives in the background
      if (tab === "shares") draw();
    } catch { /* keep the last view */ }
  }, 15000);
  const unregister = ctx.registerSearch(async (q) => (sh ? sh.shares : [])
    .filter((s) => !q || s.name.toLowerCase().includes(q))
    .map((s) => ({ title: s.name, sub: `Share · ${s.connect.windows}`, icon: "storage", action: () => ctx.navigate("storage") })));

  let following = null;
  function follow(job, title) {
    if (following) following.close();
    const drawer = ui.logDrawer(title);
    return new Promise((resolve) => {
      following = api.socket(`/ws/jobs/${job}`, {
        onMessage: (m) => {
          if (m.type === "log") drawer.log(m.line);
          else if (m.type === "done") {
            drawer.done(m.ok, m.ok ? "Done" : "Failed");
            ui.toast(m.message, m.ok ? "good" : "bad");
            load();
            ctx.refreshSummary();
            resolve(m.ok);
          }
        },
        onClose: () => resolve(null),
      });
    });
  }

  const live = () => st && st.live;
  const users = () => (sh ? sh.users : []);

  function draw() {
    clear(headActions,
      !sh.shares.length ? ui.button({ label: "Set up NAS", kind: "primary", iconName: "storage", disabled: live(), onClick: wizard }) : null,
      ui.button({ label: "New share", kind: sh.shares.length ? "primary" : "", iconName: "storage", onClick: () => shareEditor() }));
    const banners = [];
    if (live()) {
      banners.push(ui.banner("warn", "Install NimbusOS to set up storage",
        "This is the live session: nothing here is kept, so disks and the pool can't be changed. Everything else can be looked at."));
    }
    if (st.missing.length) {
      banners.push(ui.banner("bad", `Disk missing: ${st.missing.join(", ")}`,
        "A disk that was set up isn't connected. Shares on it are offline until it is back. Check its cables or power."));
    }
    const views = { shares: sharesView, disks: disksView, pool: poolView, users: usersView };
    clear(body, ...banners, ...views[tab]());
  }

  // --- shares --------------------------------------------------------------------------------
  function locationText(loc) {
    if (loc === "pool") return "The pool";
    if (loc === "system") return "System disk";
    return `Disk ${loc.slice(5)}`;
  }

  function sharesView() {
    if (!sh.shares.length) {
      return [ui.panel(null, {}, ui.emptyState({
        iconName: "storage", title: "No shares yet",
        text: "Turn spare disks, or space on this disk, into network folders that every computer at home can open.",
        action: h("div", { class: "inline" },
          ui.button({ label: "Set up NAS", kind: "primary", disabled: live(), onClick: wizard }),
          ui.button({ label: "New share", onClick: () => shareEditor() })),
      }))];
    }
    const rows = sh.shares.map((s) => {
      const [kind, label] = SECURITY[s.security];
      const used = s.usage ? `${ui.bytes(s.usage.used)}${s.usage.complete ? "" : "+"} used` : "measuring…";
      return h("tr", {},
        h("td", {}, h("div", { class: "share-name" }, h("strong", { text: s.name }),
          h("div", { class: "tags" }, ui.tag(kind, label), !s.smb ? ui.tag("", "Not shared") : null,
            !s.browseable ? ui.tag("", "Hidden") : null, s.recycle.enabled ? ui.tag("", `Recycle bin ${s.recycle.days} d`) : null,
            s.timemachine.enabled ? ui.tag("violet", "Time Machine") : null),
          s.notes ? h("span", { class: "dim", text: s.notes.split("\n")[0] }) : null)),
        h("td", {}, h("div", { class: "vm-spec" }, h("span", { class: "mono", title: s.path, text: s.path }),
          s.available ? h("span", { class: "dim", text: locationText(s.location) }) : ui.tag("bad", "Offline: disk not mounted"))),
        h("td", { class: "col-secondary" }, s.space ? h("div", { class: "vm-spec" }, h("span", { class: "num", text: used }),
          h("span", { class: "dim num", text: `${ui.bytes(s.space.free)} free` })) : h("span", { class: "dim", text: "–" })),
        h("td", { class: "col-wide" }, s.smb ? h("div", { class: "connect" },
          h("div", { class: "line" }, h("span", { class: "mono", text: s.connect.windows }), ui.copyButton(s.connect.windows, { label: "" })),
          h("div", { class: "line" }, h("span", { class: "mono", text: s.connect.mac }), ui.copyButton(s.connect.mac, { label: "" })))
          : h("span", { class: "dim", text: "–" })),
        h("td", { class: "actions" }, h("div", { class: "row-actions" },
          ui.button({ small: true, kind: "ghost", iconName: "settings", title: `Edit ${s.name}`, onClick: () => shareEditor(s) }),
          ui.button({ small: true, kind: "ghost", iconName: "x", title: `Delete ${s.name}`, onClick: () => deleteShare(s) }))));
    });
    return [ui.panel(null, { flush: true }, h("div", { class: "table-wrap" }, h("table", { class: "table share-table" },
      h("thead", {}, h("tr", {}, h("th", { text: "Share" }), h("th", { text: "Folder" }), h("th", { class: "col-secondary", text: "Usage" }),
        h("th", { class: "col-wide", text: "Connect" }), h("th", { class: "actions", text: "" }))),
      h("tbody", {}, rows))))];
  }

  function locationOptions() {
    const out = [];
    if (st.pool.mode !== "single" && st.pool.mounted) out.push({ value: "pool", label: "The pool (/mnt/user)" });
    for (const d of st.disks.filter((x) => x.role === "data" && x.label)) {
      out.push({ value: `disk:${d.label}`, label: `Disk ${d.label} (/mnt/disks/${d.label})` });
    }
    out.push({ value: "system", label: "The system disk (/srv/nimbus/shares)" });
    return out;
  }

  function shareEditor(existing = null) {
    const editing = Boolean(existing);
    const form = existing ? structuredClone(existing) : {
      name: "", location: locationOptions()[0].value, smb: true, security: "private", users: {}, browseable: true,
      recycle: { enabled: true, days: 30 }, timemachine: { enabled: false, max_gb: 0 }, hosts_allow: "", notes: "",
    };
    const rootOf = (loc) => (loc === "pool" ? "/mnt/user" : loc === "system" ? "/srv/nimbus/shares" : `/mnt/disks/${loc.slice(5)}`);
    const hint = () => `Folder: ${rootOf(form.location)}/${form.name || "<name>"}`;
    const matrix = h("div", { class: "stack-sm" });
    const drawMatrix = () => {
      if (form.security === "public") {
        clear(matrix, h("p", { class: "muted", text: "No user list: everyone has full access." }));
        return;
      }
      if (!users().length) {
        clear(matrix, h("p", { class: "muted", text: "There are no SMB users yet. Add them on the Users tab, then come back." }));
        return;
      }
      clear(matrix, h("div", { class: "user-matrix" }, users().map((u) => (form.security === "secure"
        ? ui.checkbox(`${u.name} can write`, form.users[u.name] === "rw", (on) => {
          if (on) form.users[u.name] = "rw"; else delete form.users[u.name];
        })
        : h("label", { class: "matrix-row" }, h("span", { class: "mono", text: u.name }),
          ui.select(ACCESS, form.users[u.name] || "none", { label: `Access for ${u.name}`, onChange: (v) => {
            if (v === "none") delete form.users[u.name]; else form.users[u.name] = v;
          } }))))));
    };
    const security = ui.radioCards({
      label: "Security", value: form.security,
      options: Object.entries(SECURITY).map(([value, [, title, desc]]) => ({ value, title, desc })),
      onChange: (v) => {
        form.security = v;
        if (v === "secure") form.users = Object.fromEntries(Object.entries(form.users).filter(([, a]) => a === "rw"));
        drawMatrix();
        return true;
      },
    });
    drawMatrix();
    const days = ui.input({ value: form.recycle.days, type: "number", min: 1, max: 3650, label: "Days to keep",
      onInput: (v) => { form.recycle.days = Number(v); } });
    const cap = ui.input({ value: form.timemachine.max_gb, type: "number", min: 0, label: "Size limit in GB",
      onInput: (v) => { form.timemachine.max_gb = Number(v); } });
    const notes = h("textarea", { class: "input notes", "aria-label": "Notes", rows: "2" });
    notes.value = form.notes;
    notes.addEventListener("input", () => { form.notes = notes.value; });
    const dialog = ui.modal({
      title: editing ? `Edit share ${existing.name}` : "New share", wide: true, hint: hint(),
      body: [
        h("div", { class: "grid-2" },
          ui.field("Name", ui.input({ value: form.name, mono: true, disabled: editing, label: "Share name",
            onInput: (v) => { form.name = v.trim(); dialog.setHint(hint()); } }),
          { key: "name", hint: "Becomes the folder name and the name computers see." }),
          ui.field("Location", ui.select(locationOptions(), form.location, { label: "Location",
            onChange: (v) => { form.location = v; dialog.setHint(hint()); } }),
          { key: "location", hint: editing ? "A share can't move; its files would stay behind." : "Where its files live." })),
        h("div", { class: "field", "data-field": "security" }, h("span", { text: "Who can use it" }), security),
        h("div", { class: "field", "data-field": "users" }, h("span", { text: "Users" }), matrix),
        h("div", { class: "grid-2" },
          h("div", { class: "stack-sm" },
            ui.checkbox("Shared on the network (SMB)", form.smb, (v) => { form.smb = v; }),
            ui.checkbox("Hidden: not listed when browsing the network", !form.browseable, (v) => { form.browseable = !v; }),
            ui.checkbox("Recycle bin: deleted files go to .recycle first", form.recycle.enabled, (v) => { form.recycle.enabled = v; }),
            ui.field("Keep deleted files for (days)", days, { key: "recycle.days" })),
          h("div", { class: "stack-sm" },
            ui.checkbox("Time Machine backup target (Macs)", form.timemachine.enabled, (v) => { form.timemachine.enabled = v; }),
            ui.field("Time Machine size limit (GB, 0 = none)", cap, { key: "timemachine.max_gb" }),
            ui.field("Hosts allowed", ui.input({ value: form.hosts_allow, mono: true, placeholder: "everyone, or 192.168.1.0/24",
              label: "Hosts allowed", onInput: (v) => { form.hosts_allow = v; } }), { key: "hosts_allow" }))),
        ui.field("Notes", notes, { key: "notes" })],
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }),
        ui.button({ label: editing ? "Save" : "Create share", kind: "primary", onClick: async () => {
          ui.clearInvalid(dialog.el);
          try {
            const { job } = editing ? await api.put(`/api/shares/${encodeURIComponent(existing.name)}`, form)
              : await api.post("/api/shares", form);
            dialog.close();
            follow(job, editing ? `Saving ${existing.name}` : `Creating ${form.name}`);
          } catch (error) {
            if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
          }
        } })],
    });
  }

  async function deleteShare(s) {
    let data = false;
    const ok = await ui.confirmDialog({
      title: `Delete the share ${s.name}?`, danger: true, confirmLabel: "Continue",
      message: "Computers can no longer open it. Its files stay where they are unless you tick the box.",
      details: ui.checkbox(`Also delete its files (${s.path})`, false, (v) => { data = v; }),
    });
    if (!ok) return;
    if (data) {
      const sure = await ui.confirmDialog({ title: `Delete everything in ${s.path}?`, danger: true, confirmLabel: "Delete share and files",
        message: "All files in the share are deleted for good. This can't be undone.", typeToConfirm: s.name });
      if (!sure) return;
    }
    try {
      const { job } = await api.del(`/api/shares/${encodeURIComponent(s.name)}?delete_data=${data}`);
      follow(job, `Deleting ${s.name}`);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  // --- disks ----------------------------------------------------------------------------------
  function health(d) {
    const s = d.smart;
    if (!s) return ui.pill("warn", "Unknown");
    if (s.asleep && s.passed === undefined) return ui.pill("info", "Asleep");
    if (s.passed === false) return ui.pill("bad", "Failing");
    if (s.passed === null || s.passed === undefined) {
      const p = ui.pill("", "No SMART");
      p.title = s.error || "This disk reports no SMART data.";
      return p;
    }
    const bad = (s.reallocated || 0) + (s.pending || 0) + (s.media_errors || 0);
    if (bad > 0) {
      const p = ui.pill("warn", `${bad} bad sector${bad === 1 ? "" : "s"}`);
      p.title = "Reallocated or pending sectors: back up this disk's data and plan to replace it.";
      return p;
    }
    return ui.pill("good", s.asleep ? "Healthy (asleep)" : "Healthy");
  }

  function temperature(d) {
    const t = d.smart && d.smart.temperature;
    if (t === null || t === undefined) return h("span", { class: "dim", text: "–" });
    const limit = d.smart.nvme ? settings.alerts.nvme_temp_c : settings.alerts.hdd_temp_c;
    return h("span", { class: `num ${t >= limit ? "text-bad" : t >= limit - 5 ? "text-warn" : ""}`, text: `${t} °C` });
  }

  function nextLabel(prefix) {
    const used = new Set(st.fstab.filter((e) => e.kind === "disk").map((e) => e.label));
    let n = 1;
    while (used.has(`${prefix}${n}`)) n += 1;
    return `${prefix}${n}`;
  }

  function disksView() {
    const rows = st.disks.map((d) => {
      const [kind, label] = ROLES[d.role];
      const actions = [];
      if (d.role === "unassigned" && !d.system && !live()) {
        actions.push(ui.button({ label: "Format…", small: true, kind: "danger", onClick: () => formatDisk(d) }));
        if (d.partitions.some((p) => ["xfs", "ext4", "btrfs"].includes(p.fstype) && !p.mountpoints.length)) {
          actions.push(ui.button({ label: "Add…", small: true, onClick: () => addExisting(d) }));
        }
      }
      if ((d.role === "data" || d.role === "parity") && !live()) {
        actions.push(ui.button({ small: true, kind: "ghost", iconName: "x", title: `Unmount ${d.label}`, onClick: () => unmount(d) }));
      }
      actions.push(ui.button({ small: true, kind: "ghost", iconName: "restart", title: "Read SMART again", onClick: async () => {
        try { await api.post("/api/storage/disks/smart", { device: d.path }); await load(); } catch (error) { ui.toast(ui.errorText(error), "bad"); }
      } }));
      return h("tr", {},
        h("td", {}, h("div", { class: "vm-spec" }, h("strong", { class: "mono", text: d.path }),
          h("span", { text: d.model || "Disk" }), h("span", { class: "dim mono", text: d.serial || "no serial number" }))),
        h("td", { class: "num", text: ui.bytes(d.size, 0) }),
        h("td", {}, temperature(d)),
        h("td", {}, health(d)),
        h("td", { class: "col-secondary" }, h("div", { class: "vm-spec" },
          h("span", { class: "mono", text: d.fstype || "–" }),
          d.mounts.length ? h("span", { class: "dim mono", text: d.mounts.slice(0, 2).join(", ") }) : null)),
        h("td", {}, ui.tag(kind, d.live ? "Live medium" : d.label ? `${label}: ${d.label}` : label)),
        h("td", { class: "actions" }, h("div", { class: "row-actions" }, actions)));
    });
    for (const label of st.missing) {
      rows.push(h("tr", {}, h("td", { colspan: "5" }, h("strong", { class: "mono", text: label }),
        h("span", { class: "dim", text: " is set up but not connected" })), h("td", {}, ui.pill("bad", "Missing")), h("td")));
    }
    return [ui.panel(null, { flush: true }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", { text: "Disk" }), h("th", { text: "Size" }), h("th", { text: "Temp." }), h("th", { text: "Health" }),
        h("th", { class: "col-secondary", text: "Filesystem" }), h("th", { text: "Role" }), h("th", { class: "actions", text: "" }))),
      h("tbody", {}, rows)))),
    h("p", { class: "dim", text: "The disk with the running system can't be formatted. SMART is read at most once an hour, and never wakes a sleeping disk." })];
  }

  function formatDisk(d) {
    const expected = d.serial || d.name;
    const form = { role: "data", fs: "xfs", label: nextLabel("disk") };
    const labelInput = ui.input({ value: form.label, mono: true, label: "Label", onInput: (v) => { form.label = v.trim(); } });
    const confirm = h("input", { class: "input mono", autocomplete: "off", spellcheck: "false", "aria-label": `Type ${expected} to confirm` });
    let go;
    confirm.addEventListener("input", () => { go.disabled = confirm.value.trim() !== expected; });
    const dialog = ui.modal({
      title: `Format ${d.path}`, wide: true, hint: `Mounted at ${form.role === "data" ? "/mnt/disks" : "/mnt/parity"}/<label>`,
      body: [
        ui.banner("bad", "This erases everything on the disk",
          `${d.model || "Disk"} · ${ui.bytes(d.size, 0)} · ${d.serial || "no serial"}${d.fstype ? ` · now ${d.fstype}` : ""}. All files on it are lost.`),
        h("div", { class: "field", "data-field": "role" }, h("span", { text: "Use it as" }), ui.radioCards({
          label: "Role", value: "data",
          options: [{ value: "data", title: "Data disk", desc: "Holds shares, alone or in the pool." },
            { value: "parity", title: "Parity disk", desc: "Protects the data disks (Pooled + parity). At least as large as the largest data disk." }],
          onChange: (v) => {
            form.role = v;
            form.label = nextLabel(v === "data" ? "disk" : "parity");
            labelInput.value = form.label;
            return true;
          },
        })),
        h("div", { class: "grid-2" },
          ui.field("Filesystem", ui.select([{ value: "xfs", label: "XFS (recommended)" }, { value: "ext4", label: "ext4" }, { value: "btrfs", label: "btrfs" }],
            "xfs", { label: "Filesystem", onChange: (v) => { form.fs = v; } }), { key: "fs" }),
          ui.field("Label", labelInput, { key: "label", hint: "Up to 12 lowercase letters, digits, - and _." })),
        h("label", { class: "field", "data-field": "serial" },
          h("span", {}, d.serial ? "Type the serial number " : "This disk has no serial number. Type its name ", h("strong", { class: "mono", text: expected }), " to confirm"),
          confirm)],
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }),
        go = ui.button({ label: "Erase and format", kind: "danger solid", disabled: true, onClick: async () => {
          ui.clearInvalid(dialog.el);
          try {
            const { job } = await api.post("/api/storage/disks/format", { device: d.path, serial: confirm.value.trim(), ...form });
            dialog.close();
            follow(job, `Formatting ${d.path}`);
          } catch (error) {
            if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
          }
        } })],
    });
  }

  function addExisting(d) {
    const parts = d.partitions.filter((p) => ["xfs", "ext4", "btrfs"].includes(p.fstype) && !p.mountpoints.length);
    const valid = (l) => /^[a-z0-9][a-z0-9_-]{0,11}$/.test(l || "");
    const form = { partition: parts[0].path, role: "data", label: valid(parts[0].label) ? parts[0].label : nextLabel("disk") };
    const labelInput = ui.input({ value: form.label, mono: true, label: "Label", onInput: (v) => { form.label = v.trim(); } });
    const dialog = ui.modal({
      title: `Add the filesystem on ${d.path}`, body: [
        h("p", { class: "muted", text: "Use a disk that already holds files, for example after reinstalling. Nothing on it is changed." }),
        ui.field("Partition", ui.select(parts.map((p) => ({ value: p.path, label: `${p.path} · ${p.fstype}${p.label ? ` · ${p.label}` : ""} · ${ui.bytes(p.size, 0)}` })),
          form.partition, { label: "Partition", onChange: (v) => { form.partition = v; } }), { key: "partition" }),
        ui.field("Use it as", ui.select([{ value: "data", label: "Data disk" }, { value: "parity", label: "Parity disk" }], "data",
          { label: "Role", onChange: (v) => { form.role = v; } }), { key: "role" }),
        ui.field("Label", labelInput, { key: "label" })],
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }),
        ui.button({ label: "Add", kind: "primary", onClick: async () => {
          try {
            const { job } = await api.post("/api/storage/disks/mount", form);
            dialog.close();
            follow(job, `Adding ${form.partition}`);
          } catch (error) {
            if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
          }
        } })],
    });
  }

  async function unmount(d) {
    const ok = await ui.confirmDialog({ title: `Unmount ${d.label}?`, confirmLabel: "Unmount",
      message: `The disk stops being used and can be unplugged. Its files stay on it. It must not be part of the pool or hold shares.` });
    if (!ok) return;
    try {
      follow((await api.post(`/api/storage/disks/${encodeURIComponent(d.label)}/unmount`)).job, `Unmounting ${d.label}`);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  // --- pool -------------------------------------------------------------------------------------
  function poolView() {
    const pool = st.pool;
    const form = { mode: pool.mode, policy: pool.policy, data: [...pool.data], parity: [...pool.parity] };
    const dataDisks = st.disks.filter((d) => d.role === "data" && d.label);
    const parityDisks = st.disks.filter((d) => d.role === "parity" && d.label);
    const pick = (list, key) => (list.length ? h("div", { class: "stack-sm" }, list.map((d) => ui.checkbox(
      `${d.label} · ${d.model || d.path} · ${ui.bytes(d.size, 0)}`, form[key].includes(d.label), (on) => {
        form[key] = on ? [...form[key], d.label] : form[key].filter((x) => x !== d.label);
      }))) : h("p", { class: "dim", text: key === "data" ? "No data disks yet: format one on the Disks tab." : "No parity disks yet: format one as Parity on the Disks tab." }));
    const details = h("div", { class: "stack" });
    const drawDetails = () => clear(details, form.mode === "single" ? h("p", { class: "muted",
      text: "Each share lives on one disk: the system disk or a data disk, chosen when you create the share." }) : [
      ui.field("Data disks in the pool", pick(dataDisks, "data"), { key: "data" }),
      ui.field("Where new files go", ui.select([{ value: "mfs", label: "The disk with the most free space (recommended)" },
        { value: "epmfs", label: "Keep folders together on the disk they started on" }], form.policy,
      { label: "Create policy", onChange: (v) => { form.policy = v; } }), { key: "policy" }),
      form.mode === "parity" ? ui.field("Parity disks (one or two)", pick(parityDisks, "parity"), { key: "parity" }) : null]);
    drawDetails();
    const modes = ui.radioCards({
      label: "Storage mode", value: form.mode,
      options: [
        { value: "single", title: "Single disk", desc: "Shares live on the system disk or on one data disk." },
        { value: "pooled", title: "Pooled", desc: "Data disks of any size join into one big folder, /mnt/user. Add disks later without reformatting." },
        { value: "parity", title: "Pooled + parity", disabled: !pool.snapraid,
          desc: pool.snapraid ? "Pooled, plus SnapRAID parity on one or two extra disks: one failed disk can be rebuilt." : "Needs SnapRAID: sudo apt install snapraid" },
      ],
      onChange: (v) => { form.mode = v; drawDetails(); return true; },
    });
    const out = [];
    if (pool.mode !== "single" && pool.usage) {
      const used = pool.usage.total - pool.usage.free;
      out.push(ui.panel("The pool", {}, h("div", { class: "pool-usage" },
        h("div", { class: "bar" }, h("i", { class: "fill", style: { width: `${(100 * used) / pool.usage.total}%` } })),
        h("span", { class: "num", text: `${ui.bytes(used)} used of ${ui.bytes(pool.usage.total)} · ${ui.bytes(pool.usage.free)} free` }),
        h("span", { class: "dim mono", text: `/mnt/user over ${pool.data.join(", ")}` }))));
    }
    if (pool.mode === "parity") out.push(parityCard());
    out.push(ui.panel("Storage mode", { actions: [ui.button({ label: "Apply", kind: "primary", disabled: live(), onClick: async () => {
      try {
        follow((await api.put("/api/storage/pool", form)).job, "Changing the storage mode");
      } catch (error) {
        if (!ui.markInvalid(body, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
      }
    } })] }, h("div", { class: "stack" }, h("div", { "data-field": "mode", class: "field" }, modes), details)));
    return out;
  }

  function parityCard() {
    const p = st.parity || {};
    const last = p.last || {};
    const rows = [
      ["Last sync", last.last_sync ? `${ui.timeAgo(last.last_sync)}${last.last_sync_ok === false ? " (failed)" : ""}` : "never"],
      ["Last check (scrub)", last.last_scrub ? ui.timeAgo(last.last_scrub) : "not yet"],
      ["Not yet protected", p.diff ? `${p.diff.unprotected} change${p.diff.unprotected === 1 ? "" : "s"} since the last sync` : "–"],
    ];
    const state = p.running ? ui.pill("info", "Syncing…") : last.last_sync_ok === false ? ui.pill("bad", "Last sync failed")
      : last.last_sync ? ui.pill("good", "Synced") : ui.pill("warn", "Never synced");
    return ui.panel("Parity", { actions: [state, ui.button({ label: "Sync now", small: true, disabled: p.running || live(), onClick: async () => {
      try { ui.toast((await api.post("/api/storage/parity/sync")).message, "good"); await load(); } catch (error) { ui.toast(ui.errorText(error), "bad"); }
    } })] },
    h("div", { class: "stack" },
      h("dl", { class: "facts" }, rows.map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
      last.last_sync_ok === false && last.last_sync_message ? ui.banner("bad", "SnapRAID said", last.last_sync_message) : null,
      h("p", { class: "muted", text: "Parity is scheduled, not real time: it is updated every night around 03:30 and checked weekly. "
        + "Files added or changed since the last sync can't be rebuilt yet if a disk fails." })));
  }

  // --- users -------------------------------------------------------------------------------------
  function usersView() {
    const access = (name) => sh.shares.filter((s) => s.security === "public" || s.users[name])
      .map((s) => `${s.name}${s.security === "public" ? "" : s.users[name] === "rw" ? "" : " (read)"}`);
    const add = ui.button({ label: "Add user", kind: "primary", small: true, onClick: addUser });
    const test = ui.button({ label: "Test connection", small: true, disabled: !users().length, onClick: () => testConnection() });
    if (sh.users_error) return [ui.banner("bad", "The SMB users could not be read", sh.users_error)];
    if (!users().length) {
      return [ui.panel(null, {}, ui.emptyState({ iconName: "shield", title: "No SMB users yet",
        text: "SMB users sign in to Secure and Private shares from other computers. They can't log in to this computer.",
        action: ui.button({ label: "+ Add user", kind: "primary", onClick: addUser }) }))];
    }
    const rows = users().map((u) => h("tr", {},
      h("td", {}, h("strong", { class: "mono", text: u.name })),
      h("td", { text: u.desktop ? "Also a desktop account" : "Network only" }),
      h("td", { class: "col-secondary", text: access(u.name).join(", ") || "No shares yet" }),
      h("td", { class: "actions" }, h("div", { class: "row-actions" },
        ui.button({ small: true, kind: "ghost", iconName: "shield", title: `New password for ${u.name}`, onClick: () => setPassword(u.name) }),
        ui.button({ small: true, kind: "ghost", iconName: "x", title: `Remove ${u.name}`, onClick: () => removeUser(u.name) })))));
    return [ui.panel("SMB users", { actions: [test, add], flush: true }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", { text: "User" }), h("th", { text: "Kind" }), h("th", { class: "col-secondary", text: "Shares" }),
        h("th", { class: "actions", text: "" }))),
      h("tbody", {}, rows))))];
  }

  function passwordFields(form) {
    const a = ui.input({ type: "password", label: "Password", onInput: (v) => { form.password = v; } });
    const b = ui.input({ type: "password", label: "Repeat the password", onInput: (v) => { form.repeat = v; } });
    return [ui.field("Password", a, { key: "password", hint: "At least 8 characters." }), ui.field("Repeat the password", b, { key: "repeat" })];
  }

  function addUser() {
    const form = { name: "", password: "", repeat: "" };
    const dialog = ui.modal({ title: "Add an SMB user", body: [
      ui.field("User name", ui.input({ mono: true, label: "User name", onInput: (v) => { form.name = v.trim(); } }),
        { key: "name", hint: "Lowercase letters, digits, - and _. If it is your desktop account's name, that account gets network access too." }),
      ...passwordFields(form)],
    actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }), ui.button({ label: "Add", kind: "primary", onClick: async () => {
      ui.clearInvalid(dialog.el);
      if (form.password !== form.repeat) { ui.markInvalid(dialog.el, "repeat", "The two passwords are not the same."); return; }
      try {
        ui.toast((await api.post("/api/smbusers", { name: form.name, password: form.password })).message, "good");
        dialog.close();
        await load();
      } catch (error) {
        if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
      }
    } })] });
  }

  function setPassword(name) {
    const form = { password: "", repeat: "" };
    const dialog = ui.modal({ title: `New password for ${name}`, body: passwordFields(form),
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }), ui.button({ label: "Save", kind: "primary", onClick: async () => {
        ui.clearInvalid(dialog.el);
        if (form.password !== form.repeat) { ui.markInvalid(dialog.el, "repeat", "The two passwords are not the same."); return; }
        try {
          ui.toast((await api.put(`/api/smbusers/${encodeURIComponent(name)}/password`, { password: form.password })).message, "good");
          dialog.close();
        } catch (error) {
          if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
        }
      } })] });
  }

  async function removeUser(name) {
    const ok = await ui.confirmDialog({ title: `Remove ${name}?`, danger: true, confirmLabel: "Remove",
      message: `${name} can no longer sign in to shares and is taken off every share's list. Files they made stay.` });
    if (!ok) return;
    try {
      ui.toast((await api.del(`/api/smbusers/${encodeURIComponent(name)}`)).message, "good");
      await load();
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  function testConnection(preset = null) {
    const form = { user: preset ? preset.user : (users()[0] || {}).name, password: preset ? preset.password : "" };
    const result = h("div");
    const dialog = ui.modal({ title: "Test connection", body: [
      h("p", { class: "muted", text: "Signs in to this computer's Samba the way another computer would, and lists the shares the user sees." }),
      ui.field("User", ui.select(users().map((u) => ({ value: u.name, label: u.name })), form.user, { label: "User",
        onChange: (v) => { form.user = v; } }), { key: "user" }),
      preset ? null : ui.field("Password", ui.input({ type: "password", label: "Password", onInput: (v) => { form.password = v; } }), { key: "password" }),
      result],
    actions: [ui.button({ label: "Close", onClick: () => dialog.close() }), ui.button({ label: "Test", kind: "primary", onClick: async () => {
      try {
        const r = await api.post("/api/smbusers/test", form);
        clear(result, ui.banner(r.ok ? "good" : "bad", r.message, r.shares.length ? `Shares: ${r.shares.join(", ")}` : null));
      } catch (error) {
        clear(result, ui.banner("bad", "The test could not run", ui.errorText(error)));
      }
    } })] });
  }

  // --- the NAS wizard ------------------------------------------------------------------------------
  function wizard() {
    const free = st.disks.filter((d) => d.role === "unassigned" && !d.system);
    const ready = st.disks.filter((d) => d.role === "data" && d.label);
    const form = { system: !free.length && !ready.length, picked: new Set(), existing: new Set(ready.map((d) => d.label)),
      mode: "single", parity: "", shares: new Set(Object.keys(WIZARD_SHARES)), user: { name: "", password: "", repeat: "" }, serials: {} };
    let step = 0;
    let finished = false;
    const steps = ["Disks", "Mode", "Shares", "User", "Confirm"];
    const content = h("div", { class: "stack" });
    const progress = h("ol", { class: "wizard-steps" });
    let back;
    let next;
    const dialog = ui.modal({ title: "Set up NAS", wide: true, body: [progress, content],
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }),
        back = ui.button({ label: "Back", onClick: () => { step -= 1; show(); } }),
        next = ui.button({ label: "Next", kind: "primary", onClick: () => advance() })] });

    const selectedDisks = () => free.filter((d) => form.picked.has(d.path));
    const dataCount = () => form.existing.size + selectedDisks().length - (form.mode === "parity" && form.parity ? 1 : 0);

    function show() {
      clear(progress, steps.map((s, i) => h("li", { class: i === step ? "current" : i < step ? "done" : "", text: s })));
      back.hidden = step === 0;
      next.querySelector("span").textContent = step === 4 ? (selectedDisks().length ? "Erase disks and set up" : "Set up") : "Next";
      next.className = `btn ${step === 4 && selectedDisks().length ? "danger solid" : "primary"}`;
      ui.clearInvalid(dialog.el);
      clear(content, ...[stepDisks, stepMode, stepShares, stepUser, stepConfirm][step]());
    }

    function stepDisks() {
      return [
        h("p", { class: "muted", text: "Where should the shared files live?" }),
        ui.radioCards({ label: "Where", value: form.system ? "system" : "disks",
          options: [{ value: "disks", title: "Extra disks", desc: "Use the disks you pick below. New disks are erased and formatted.", disabled: !free.length && !ready.length },
            { value: "system", title: "The system disk", desc: "No extra disks: shares live in /srv/nimbus/shares on this disk." }],
          onChange: (v) => { form.system = v === "system"; show(); return true; } }),
        form.system ? null : h("div", { class: "stack-sm", "data-field": "disks" },
          ...ready.map((d) => ui.checkbox(`${d.label} · already set up · ${d.model || d.path} · ${ui.bytes(d.size, 0)}`, form.existing.has(d.label),
            (on) => { if (on) form.existing.add(d.label); else form.existing.delete(d.label); })),
          ...free.map((d) => ui.checkbox(`${d.path} · ${d.model || "disk"} · ${ui.bytes(d.size, 0)}${d.fstype ? ` · has ${d.fstype} (will be erased)` : ""}`,
            form.picked.has(d.path), (on) => { if (on) form.picked.add(d.path); else form.picked.delete(d.path); })))];
    }

    function stepMode() {
      const n = form.system ? 0 : form.existing.size + selectedDisks().length;
      if (form.system) form.mode = "single";
      const largest = selectedDisks().slice().sort((a, b) => b.size - a.size)[0];
      if (form.mode === "parity" && !form.parity && largest) form.parity = largest.path;
      return [
        ui.radioCards({ label: "Mode", value: form.mode,
          options: [
            { value: "single", title: "Single disk", desc: form.system ? "The system disk." : "One disk holds the shares.", disabled: !form.system && n !== 1 },
            { value: "pooled", title: "Pooled", desc: "All chosen disks join into one big folder.", disabled: form.system || n < 1 },
            { value: "parity", title: "Pooled + parity", disabled: form.system || n < 2 || !st.pool.snapraid || !selectedDisks().length,
              desc: "One new disk protects the others (SnapRAID). Parity is updated nightly, not in real time." }],
          onChange: (v) => { form.mode = v; show(); return true; } }),
        form.mode === "parity" ? ui.field("Parity disk (at least as large as the largest data disk)", ui.select(
          selectedDisks().map((d) => ({ value: d.path, label: `${d.path} · ${ui.bytes(d.size, 0)}` })), form.parity,
          { label: "Parity disk", onChange: (v) => { form.parity = v; } }), { key: "parity" }) : null];
    }

    function stepShares() {
      return [h("p", { class: "muted", text: "Which shares should be made? You can add more, and change these, later." }),
        h("div", { class: "stack-sm", "data-field": "shares" }, Object.entries(WIZARD_SHARES).map(([name, desc]) =>
          ui.checkbox(`${name} · ${desc}`, form.shares.has(name), (on) => { if (on) form.shares.add(name); else form.shares.delete(name); })))];
    }

    function stepUser() {
      return [h("p", { class: "muted", text: "The first SMB user: the name and password other computers sign in with." }),
        ui.field("User name", ui.input({ value: form.user.name, mono: true, label: "User name", onInput: (v) => { form.user.name = v.trim(); } }), { key: "user.name" }),
        ...passwordFields(form.user).map((f) => { f.dataset.field = f.dataset.field === "password" ? "user.password" : "repeat"; return f; })];
    }

    function diskSummary() {
      if (form.system) return "The system disk (/srv/nimbus/shares)";
      const kept = [...form.existing].map((l) => `${l} as it is`);
      const fresh = selectedDisks().map((d) => `${d.path} → ${form.mode === "parity" && d.path === form.parity ? "parity" : "data"} (erased)`);
      return [...kept, ...fresh].join(", ") || "none";
    }

    function stepConfirm() {
      const erase = selectedDisks();
      const names = { single: "Single disk", pooled: "Pooled", parity: "Pooled + parity" };
      return [
        h("dl", { class: "facts" }, [["Mode", names[form.mode]], ["Disks", diskSummary()],
          ["Shares", [...form.shares].join(", ") || "none"], ["User", form.user.name || "–"]]
          .map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })])),
        erase.length ? ui.banner("bad", `${erase.length} disk${erase.length === 1 ? "" : "s"} will be erased`,
          "Everything on them is lost. Type each serial number to confirm.") : null,
        ...erase.map((d, i) => h("label", { class: "field", "data-field": `disks.${i}.serial` },
          h("span", {}, `${d.path} · ${d.model || "disk"} · ${ui.bytes(d.size, 0)} (${d.path === form.parity && form.mode === "parity" ? "parity" : "data"}): type `,
            h("strong", { class: "mono", text: d.serial || d.name })),
          ui.input({ value: form.serials[d.path] || "", mono: true, label: `Serial of ${d.path}`,
            onInput: (v) => { form.serials[d.path] = v; } })))];
    }

    async function advance() {
      if (finished) { dialog.close(); return; }
      ui.clearInvalid(dialog.el);
      if (step === 0 && !form.system && !form.existing.size && !form.picked.size) {
        ui.markInvalid(dialog.el, "disks", "Pick at least one disk, or use the system disk.");
        return;
      }
      if (step === 1) {
        const n = form.existing.size + selectedDisks().length;
        if (!form.system && form.mode === "single" && n !== 1) { form.mode = n > 1 ? "pooled" : "single"; }
      }
      if (step === 3) {
        if (form.user.password !== form.user.repeat) { ui.markInvalid(dialog.el, "repeat", "The two passwords are not the same."); return; }
      }
      if (step < 4) { step += 1; show(); return; }
      const payload = {
        mode: form.mode, system: form.system, existing: form.system ? [] : [...form.existing],
        disks: form.system ? [] : selectedDisks().map((d) => ({ device: d.path, serial: (form.serials[d.path] || "").trim(),
          role: form.mode === "parity" && d.path === form.parity ? "parity" : "data" })),
        shares: [...form.shares], user: { name: form.user.name, password: form.user.password },
      };
      try {
        const { job } = await api.post("/api/storage/wizard", payload);
        clear(content, h("div", { class: "loading", text: "Setting up… the log is in the drawer." }));
        back.hidden = true;
        next.hidden = true;
        const ok = await follow(job, "Setting up the NAS");
        if (ok) done(); else clear(content, ui.banner("bad", "Setting up did not finish", "The log in the drawer says why. Fix it and run the wizard again."));
      } catch (error) {
        const field = error.field || "";
        step = field.startsWith("user") ? 3 : field.startsWith("disks") && field.endsWith("serial") ? 4 : field.startsWith("disks") ? 0 : field === "mode" ? 1 : step;
        show();
        if (!ui.markInvalid(dialog.el, field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
      }
    }

    async function done() {
      clear(progress);
      await load();
      clear(content, ui.banner("good", "Your NAS is ready", "Open the shares from any computer on your network:"),
        h("div", { class: "stack-sm" }, sh.shares.map((s) => h("div", { class: "connect line" }, h("strong", { text: s.name }),
          h("span", { class: "mono", text: s.connect.windows }), ui.copyButton(s.connect.windows, { label: "" }),
          h("span", { class: "mono", text: s.connect.mac }), ui.copyButton(s.connect.mac, { label: "" })))),
        ui.button({ label: "Test connection", iconName: "check", onClick: () => testConnection({ user: form.user.name, password: form.user.password }) }));
      next.hidden = false;
      next.querySelector("span").textContent = "Close";
      next.className = "btn primary";
      finished = true;
    }

    show();
  }

  await load();
  const intent = ctx.takeIntent("storage");
  const tabIndex = ["shares", "disks", "pool", "users"].indexOf(intent && intent.tab);
  if (tabIndex > 0) tabs.querySelectorAll("button")[tabIndex].click();
  return () => { alive = false; clearInterval(timer); unregister(); };
}
