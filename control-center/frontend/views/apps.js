// =============================================================================
// views/apps.js: the Apps page (Docker App Store)
// =============================================================================
// What: - Installed: one row per app with its icon, status (Running, Starting,
//         Stopped, Error with the reason, or the job it is busy with), ports,
//         and actions: Open (web UI), Start/Stop, Restart, Logs (live), Update
//         (pull and recreate), Edit, Uninstall (data kept unless "delete
//         data" is ticked and the name typed). "Template update available"
//         shows the Compose diff before anything changes;
//       - Catalog: every app with Install (1-click: smart defaults, free ports,
//         generated secrets, shares bound by hint) and Advanced;
//       - Advanced install / Edit: name, ports (add, edit, remove), volumes
//         (folder or "pick a share"), environment (secrets hidden until
//         shown), network mode, restart policy, and a Compose file tab
//         (checked with docker compose config before deploying); the field an
//         error is about is highlighted;
//       - every job (install, update, ...) streams its log into the drawer.
//       Refreshes the status every 5 seconds while open.
// Why:  spec Tab A; docs/PLAN.md NOTE-30 and NOTE-66.
// =============================================================================

const STATES = {
  running: ["good", "Running"], starting: ["info", "Starting"], stopped: ["", "Stopped"],
  error: ["bad", "Error"], unknown: ["warn", "Unknown"],
};
const BUSY = {
  install: "Installing", edit: "Applying", start: "Starting", stop: "Stopping", restart: "Restarting",
  update: "Updating", "template update": "Updating", uninstall: "Removing",
};
const RESTARTS = [
  { value: "unless-stopped", label: "Unless stopped (recommended)" }, { value: "always", label: "Always" },
  { value: "on-failure", label: "On failure" }, { value: "no", label: "Never" },
];

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const { h, clear } = ui;
  let tab = "installed";
  let state = { apps: [], docker: { ok: true } };
  let catalog = { apps: [], errors: [] };
  let alive = true;

  const body = h("div", { class: "stack" }, h("div", { class: "loading", text: "Loading apps…" }));
  const tabs = ui.segmented({
    label: "Apps", value: tab,
    options: [{ value: "installed", title: "Installed" }, { value: "catalog", title: "Catalog" }],
    onChange: (value) => { tab = value; draw(); },
  });
  const showCatalog = () => { tab = "catalog"; tabs.querySelectorAll("button")[1].click(); };
  root.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: "Apps" }),
        h("p", { class: "desc", text: "Docker apps in one click. Each app keeps its data in its own folder." })),
      h("div", { class: "actions" }, ui.button({ label: "Install an app", kind: "primary", iconName: "download", onClick: showCatalog }))),
    h("div", { style: { marginBottom: "16px" } }, tabs),
    body);

  // --- data --------------------------------------------------------------------
  async function load() {
    try {
      [state, catalog] = await Promise.all([api.get("/api/apps"), api.get("/api/catalog")]);
      if (alive) draw();
    } catch (error) {
      clear(body, ui.banner("bad", "The apps could not be read", ui.errorText(error)));
    }
  }
  async function refresh() {
    try {
      state = await api.get("/api/apps");
      if (alive && tab === "installed" && !document.querySelector(".backdrop")) draw();
    } catch { /* keep the last view */ }
  }
  const timer = setInterval(refresh, 5000);
  const unregister = ctx.registerSearch(async (q) => {
    const match = (s) => !q || s.toLowerCase().includes(q);
    return [
      ...state.apps.filter((a) => match(a.name) || match(a.app_name))
        .map((a) => ({ title: a.name, sub: `App · ${a.app_name}`, icon: "apps", action: () => ctx.navigate("apps") })),
      ...catalog.apps.filter((a) => q && (match(a.name) || match(a.category)))
        .map((a) => ({ title: `Install ${a.name}`, sub: "Catalog", icon: "download", action: () => { ctx.navigate("apps"); showCatalog(); } })),
    ];
  });

  function draw() {
    clear(body, ...(tab === "installed" ? installed() : catalogView()));
  }

  // --- installed ---------------------------------------------------------------------
  function installed() {
    const out = [];
    if (!state.docker.ok) out.push(ui.banner("bad", "Docker is not available", state.docker.message));
    if (!state.apps.length) {
      out.push(ui.panel(null, {}, ui.emptyState({
        iconName: "apps", title: "No apps yet",
        text: "Installed apps appear here with their status, ports and a link to their web page.",
        action: ui.button({ label: "+ Install an app", kind: "primary", onClick: showCatalog }),
      })));
      return out;
    }
    const rows = state.apps.map((app) => {
      const [kind, label] = app.busy ? ["info", `${BUSY[app.busy] || "Working"}…`] : (STATES[app.state] || STATES.unknown);
      const status = ui.pill(kind, label);
      if (app.reason) status.title = app.reason;
      const running = ["running", "starting"].includes(app.state);
      const actions = [];
      if (app.busy) {
        actions.push(ui.button({ label: "Progress", small: true, iconName: "terminal", onClick: () => follow(app.job, `${BUSY[app.busy] || "Working on"} ${app.name}`) }));
      } else {
        if (app.webui && running) actions.push(ui.button({ label: "Open", small: true, iconName: "external", onClick: () => window.open(app.webui, "_blank", "noopener") }));
        if (!running) actions.push(ui.button({ label: "Start", small: true, iconName: "play", onClick: () => act(app.name, "start") }));
        else actions.push(ui.button({ small: true, iconName: "stop", kind: "ghost", title: "Stop", onClick: () => act(app.name, "stop") }));
      }
      if (!app.busy) actions.push(
        ui.button({ small: true, iconName: "restart", kind: "ghost", title: "Restart", onClick: () => act(app.name, "restart") }),
        ui.button({ small: true, iconName: "terminal", kind: "ghost", title: "Logs", onClick: () => logs(app.name) }),
        ui.button({ small: true, iconName: "download", kind: "ghost", title: "Update: pull new images and recreate", onClick: () => act(app.name, "update") }),
        ui.button({ small: true, iconName: "settings", kind: "ghost", title: "Edit settings", onClick: () => advanced(app.app, app.name) }),
        ui.button({ small: true, iconName: "x", kind: "ghost", title: "Uninstall", onClick: () => uninstall(app) }));
      const ports = app.network_mode === "host" ? h("span", { class: "dim", text: "host network" })
        : h("span", { class: "port-list mono" }, app.ports.map((p) => h("span", { text: `${p.ip ? `${p.ip}:` : ""}${p.host}→${p.container}${p.protocol === "udp" ? "/udp" : ""}` })));
      return h("tr", {},
        h("td", {}, h("div", { class: "app-name" }, appIcon(app.icon, app.app_name),
          h("div", { class: "names" }, h("strong", { text: app.name, title: app.name }), h("span", { text: app.app_name })))),
        h("td", {}, h("div", { class: "status-cell" }, status,
          app.state === "error" && app.reason && !app.busy ? h("span", { class: "reason", text: app.reason }) : null,
          app.template_update && !app.busy ? ui.button({ label: "Template update", small: true, kind: "ghost", iconName: "info",
            title: "A newer version of this app's template is available; see what changes", onClick: () => templateUpdate(app.name) }) : null)),
        h("td", { class: "col-secondary" }, ports),
        h("td", { class: "actions" }, h("div", { class: "row-actions" }, actions)));
    });
    out.push(ui.panel(null, { flush: true }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", { text: "App" }), h("th", { text: "Status" }), h("th", { class: "col-secondary", text: "Ports" }), h("th", { class: "actions", text: "" }))),
      h("tbody", {}, rows)))));
    return out;
  }

  // The catalog icon, or the app name's first letter when it has none.
  const appIcon = (src, name, large = false) => (src ? h("img", { class: large ? "app-icon lg" : "app-icon", src, alt: "" })
    : h("span", { class: large ? "app-icon lg letter" : "app-icon letter", "aria-hidden": "true", text: (name || "?").charAt(0).toUpperCase() }));

  // --- catalog ---------------------------------------------------------------------------
  function catalogView() {
    const out = catalog.errors.map((e) => ui.banner("warn", "A catalog app could not be read", e));
    if (!catalog.apps.length) {
      out.push(ui.panel(null, {}, ui.emptyState({ iconName: "apps", title: "The catalog is empty",
        text: "Install the nimbus-catalog package: sudo apt install nimbus-catalog" })));
      return out;
    }
    out.push(h("div", { class: "catalog-grid" }, catalog.apps.map((app) => h("article", { class: "panel app-card" },
      h("header", {}, appIcon(app.icon, app.name, true), h("div", {}, h("h3", { text: app.name }), ui.tag("", app.category))),
      h("p", { text: app.description }),
      h("footer", {},
        ui.button({ label: "Install", kind: "primary", small: true, onClick: () => oneClick(app) }),
        ui.button({ label: "Advanced…", small: true, onClick: () => advanced(app.id) }),
        app.installed_as.length ? h("span", { class: "dim", text: `Installed as ${app.installed_as.join(", ")}` }) : null)))));
    return out;
  }

  // --- jobs -----------------------------------------------------------------------------------
  // One drawer at a time; opening another stops following the last one.
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

  async function act(name, action) {
    const titles = { start: "Starting", stop: "Stopping", restart: "Restarting", update: "Updating" };
    try {
      const { job } = await api.post(`/api/apps/${encodeURIComponent(name)}/actions/${action}`);
      follow(job, `${titles[action]} ${name}`);
      refresh();
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  async function oneClick(app) {
    try {
      const { job, name } = await api.post("/api/apps", { app: app.id });
      ui.toast(`Installing ${app.name} as '${name}'…`, "info");
      tab = "installed";
      tabs.querySelectorAll("button")[0].click();
      follow(job, `Installing ${app.name}`);
    } catch (error) {
      // A port or setting needs a decision: open Advanced at that field.
      if (error.field) advanced(app.id, null, error);
      else ui.toast(ui.errorText(error), "bad");
    }
  }

  function logs(name) {
    const pre = h("pre", { class: "log", tabindex: "0" });
    let ws = null;
    const m = ui.modal({ title: `Logs: ${name}`, wide: true, body: pre, hint: "Live; the last 200 lines first.",
      actions: [ui.button({ label: "Close", onClick: () => m.close() })], onClose: () => ws && ws.close() });
    ws = api.socket(`/ws/apps/${encodeURIComponent(name)}/logs`, {
      onMessage: (msg) => {
        const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
        pre.appendChild(document.createTextNode(`${msg.type === "log" ? msg.line : `— ${msg.message} —`}\n`));
        if (stick) pre.scrollTop = pre.scrollHeight;
      },
    });
  }

  async function uninstall(app) {
    let deleteData = false;
    const note = h("p", { class: "muted", text: `Its data stays in ${app.path} unless you tick the box.` });
    const box = ui.checkbox(`Also delete the app's data in ${app.path}`, false, (v) => { deleteData = v; });
    const first = await ui.confirmDialog({ title: `Uninstall ${app.name}?`, confirmLabel: "Continue", danger: true,
      message: `${app.app_name} and its containers are removed.`, details: h("div", { class: "stack" }, note, box) });
    if (!first) return;
    if (deleteData) {
      const sure = await ui.confirmDialog({ title: `Delete ${app.name}'s data?`, danger: true, confirmLabel: "Uninstall and delete data",
        message: `Everything in ${app.path} is deleted for good: settings, databases and uploaded files.`, typeToConfirm: app.name });
      if (!sure) return;
    }
    try {
      const { job } = await api.del(`/api/apps/${encodeURIComponent(app.name)}?delete_data=${deleteData}`);
      follow(job, `Uninstalling ${app.name}`);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  async function templateUpdate(name) {
    let diff;
    try {
      diff = await api.get(`/api/apps/${encodeURIComponent(name)}/template-diff`);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
      return;
    }
    const pre = h("pre", { class: "diff" }, diff.diff.length ? diff.diff.map((line) => h("span", {
      class: line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "",
      text: `${line}\n` })) : "The new template gives the same file.");
    const m = ui.modal({
      title: `Template update for ${name}`, wide: true, hint: `Template version ${diff.from} → ${diff.to}`,
      body: [diff.custom ? ui.banner("warn", "This app's Compose file was edited by hand", "Applying the template replaces those edits with the lines shown in green.") : null,
        h("p", { class: "muted", text: "Nothing changes until you apply it. Your data stays where it is." }), pre],
      actions: [ui.button({ label: "Not now", onClick: () => m.close() }),
        ui.button({ label: "Apply template", kind: "primary", onClick: async () => {
          try {
            const { job } = await api.post(`/api/apps/${encodeURIComponent(name)}/template-update`);
            m.close();
            follow(job, `Updating ${name}'s template`);
          } catch (error) { ui.toast(ui.errorText(error), "bad"); }
        } })],
    });
  }

  // --- advanced install / edit -------------------------------------------------------------------
  async function advanced(appId, name = null, firstError = null) {
    let plan;
    let options;
    try {
      [plan, options] = await Promise.all([
        name ? api.get(`/api/apps/${encodeURIComponent(name)}`) : api.post("/api/apps/plan", { app: appId }),
        api.get("/api/apps/options"),
      ]);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
      return;
    }
    const editing = Boolean(name);
    const manifest = plan.manifest || { ports: [], volumes: [], env: [], name: appId };
    const inputs = structuredClone(editing ? plan.record.inputs : plan.inputs);
    // The environment is edited as rows (a name can change while typing) and
    // sent as an object.
    const envRows = Object.entries(inputs.env).map(([key, value]) => ({ name: key, value }));
    const collect = () => ({ ...inputs, env: Object.fromEntries(envRows.filter((r) => r.name || r.value).map((r) => [r.name, r.value])) });
    const secretNames = new Set(manifest.env.filter((e) => e.secret).map((e) => e.name));
    let composeText = plan.compose;
    let composeEdited = editing && plan.record.custom;
    let view = composeEdited ? "compose" : "settings";

    const settingsEl = h("div", { class: "stack" });
    const composeArea = h("textarea", { class: "input", spellcheck: "false", "aria-label": "docker-compose.yml" });
    composeArea.value = composeText;
    composeArea.addEventListener("input", () => { composeEdited = true; composeText = composeArea.value; });
    const composeEl = h("div", { class: "stack", hidden: view !== "compose", "data-field": "compose" },
      h("p", { class: "muted", text: "Edit the Compose file directly. Bind mounts must stay inside the app's folder, a share, or the host paths the app needs. After a direct edit, the Settings tab no longer applies to this app." }),
      composeArea,
      h("div", { class: "inline" }, ui.button({ label: "Check the file", small: true, iconName: "check", onClick: validate })));
    settingsEl.hidden = view !== "settings";

    const switcher = ui.segmented({
      label: "Install settings", value: view,
      options: [{ value: "settings", title: "Settings" }, { value: "compose", title: "Compose file" }],
      onChange: async (v) => {
        view = v;
        if (v === "compose" && !composeEdited) {
          try {
            composeText = (await api.post("/api/apps/preview", { app: appId, inputs: collect() })).compose;
            composeArea.value = composeText;
          } catch (error) { ui.markInvalid(dialog.el, error.field, ui.errorText(error)); }
        }
        settingsEl.hidden = v !== "settings";
        composeEl.hidden = v !== "compose";
      },
    });

    const drawSettings = () => clear(settingsEl,
      ...(plan.warnings || []).map((w) => ui.banner("warn", "Check this before installing", w)),
      plan.notes ? ui.banner("info", `About ${manifest.name}`, plan.notes) : null,
      plan.existing_data ? ui.banner("info", "Existing data found", `${plan.path} already exists; the app will use what is there.`) : null,
      ui.field("Name", ui.input({ value: inputs.name, mono: true, disabled: editing, onInput: (v) => { inputs.name = v; } }),
        { key: "name", hint: "Lowercase letters, digits, - and _. Also the folder name." }),
      inputs.network_mode === "host" ? null : section("Ports", "Add port", () => { inputs.ports.push({ container: 80, host: 8080, protocol: "tcp", ip: "" }); drawSettings(); },
        ["Container port", "Host port", "Protocol", "Address (empty: all)", ""], "ports",
        inputs.ports.map((p, i) => h("div", { class: "row ports" },
          ui.field("Container port", ui.input({ value: p.container, type: "number", min: 1, max: 65535, label: `Port ${i + 1}: container port`,
            onInput: (v) => { p.container = Number(v); } }), { key: `ports.${i}.container` }),
          ui.field("Host port", ui.input({ value: p.host, type: "number", min: 1, max: 65535, label: `Port ${i + 1}: host port`,
            onInput: (v) => { p.host = Number(v); } }), { key: `ports.${i}.host` }),
          ui.field("Protocol", ui.select([{ value: "tcp", label: "TCP" }, { value: "udp", label: "UDP" }], p.protocol,
            { label: `Port ${i + 1}: protocol`, onChange: (v) => { p.protocol = v; } }), { key: `ports.${i}.protocol` }),
          ui.field("Address", ui.input({ value: p.ip, mono: true, placeholder: "all addresses", label: `Port ${i + 1}: address`,
            onInput: (v) => { p.ip = v; } }), { key: `ports.${i}.ip` }),
          removeButton(() => { inputs.ports.splice(i, 1); drawSettings(); })))),
      section("Volumes", "Add volume", () => { inputs.volumes.push({ container: "/data", source: `${plan.path}/data`, read_only: false }); drawSettings(); },
        ["Container path", "Folder on this computer", "Share", "", ""], "volumes",
        inputs.volumes.map((v, i) => {
          const source = ui.input({ value: v.source, mono: true, label: `Volume ${i + 1}: folder on this computer`, onInput: (val) => { v.source = val; } });
          return h("div", { class: "row volumes" },
            ui.field("Container path", ui.input({ value: v.container, mono: true, label: `Volume ${i + 1}: container path`,
              onInput: (val) => { v.container = val; } }), { key: `volumes.${i}.container` }),
            ui.field("Folder", source, { key: `volumes.${i}.source` }),
            ui.select([{ value: "", label: options.shares.length ? "Pick a share…" : "No shares yet" },
              ...options.shares.map((s) => ({ value: s.path, label: s.name }))], "", {
              label: `Volume ${i + 1}: pick a share`, onChange: (path) => { if (path) { v.source = path; source.value = path; } } }),
            ui.checkbox("Read-only", v.read_only, (on) => { v.read_only = on; }),
            removeButton(() => { inputs.volumes.splice(i, 1); drawSettings(); }));
        })),
      section("Environment", "Add variable", () => { envRows.push({ name: "", value: "" }); drawSettings(); },
        ["Name", "Value", ""], "env",
        envRows.map((row, i) => {
          const secret = secretNames.has(row.name);
          const valueInput = ui.input({ value: row.value, mono: true, type: secret ? "password" : "text", label: `Value of variable ${i + 1}`,
            onInput: (v) => { row.value = v; } });
          const valueField = ui.field("Value", secret ? h("div", { class: "inline" }, valueInput,
            ui.button({ small: true, kind: "ghost", iconName: "eye", title: "Show or hide the value",
              onClick: () => { valueInput.type = valueInput.type === "password" ? "text" : "password"; } })) : valueInput,
          { key: `env.${row.name}` });
          const nameField = ui.field("Name", ui.input({ value: row.name, mono: true, label: `Name of variable ${i + 1}`,
            onInput: (v) => {
              row.name = v;
              nameField.dataset.field = `env.${v}.name`;      // errors name the variable
              valueField.dataset.field = `env.${v}`;
            } }), { key: `env.${row.name}.name` });
          return h("div", { class: "row env" }, nameField, valueField,
            removeButton(() => { envRows.splice(i, 1); drawSettings(); }));
        })),
      h("div", { class: "grid-2" },
        ui.field("Network", networkControl(), { key: "network_mode", hint: "Bridge gives the app its own network with published ports; Host shares this computer's network." }),
        ui.field("Restart", ui.select(RESTARTS, inputs.restart, { onChange: (v) => { inputs.restart = v; } }), { key: "restart" })));

    function networkControl() {
      const known = ["bridge", "host", ...options.networks];
      const custom = ui.input({ value: known.includes(inputs.network_mode) ? "" : inputs.network_mode, mono: true, placeholder: "network name",
        onInput: (v) => { inputs.network_mode = v; } });
      custom.hidden = known.includes(inputs.network_mode);
      const sel = ui.select([{ value: "bridge", label: "Bridge (default)" }, { value: "host", label: "Host network" },
        ...options.networks.map((n) => ({ value: n, label: `Network ${n}` })), { value: "__other", label: "Other network…" }],
      known.includes(inputs.network_mode) ? inputs.network_mode : "__other", {
        onChange: (v) => {
          custom.hidden = v !== "__other";
          const wasHost = inputs.network_mode === "host";
          inputs.network_mode = v === "__other" ? custom.value : v;
          if (wasHost !== (inputs.network_mode === "host")) drawSettings();
        } });
      return h("div", { class: "inline" }, sel, custom);
    }

    function section(title, addLabel, onAdd, labels, key, rows) {
      return h("div", { class: "form-section", "data-field": key },
        h("div", { class: "section-head" }, h("span", { class: "label", text: title }),
          ui.button({ label: addLabel, small: true, kind: "ghost", onClick: onAdd })),
        rows.length ? h("div", { class: `row-labels row ${key}` }, labels.map((l) => h("span", { text: l }))) : null,
        rows.length ? h("div", { class: "rows" }, rows) : h("p", { class: "dim", text: `No ${title.toLowerCase()}.` }));
    }

    function removeButton(onClick) {
      return ui.button({ small: true, kind: "ghost", iconName: "x", title: "Remove", onClick });
    }

    async function validate() {
      ui.clearInvalid(dialog.el);
      try {
        await api.post("/api/apps/validate", { app: appId, name: inputs.name, compose: composeArea.value });
        ui.toast("The Compose file is valid.", "good");
      } catch (error) {
        ui.markInvalid(dialog.el, "compose", ui.errorText(error));
      }
    }

    async function submit() {
      ui.clearInvalid(dialog.el);
      const payload = composeEdited ? { inputs: collect(), compose: composeArea.value } : { inputs: collect() };
      try {
        const result = editing
          ? await api.put(`/api/apps/${encodeURIComponent(name)}`, payload)
          : await api.post("/api/apps", { app: appId, ...payload });
        dialog.close();
        tab = "installed";
        tabs.querySelectorAll("button")[0].click();
        follow(result.job, editing ? `Applying ${name}` : `Installing ${manifest.name}`);
      } catch (error) {
        showError(error);
      }
    }

    function showError(error) {
      const goCompose = error.field === "compose";
      if (goCompose !== (view === "compose")) switcher.querySelectorAll("button")[goCompose ? 1 : 0].click();
      if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
    }

    drawSettings();
    const dialog = ui.modal({
      title: editing ? `Edit ${name}` : `Install ${manifest.name}`, wide: true,
      hint: `Saved to ${plan.path}/docker-compose.yml`,
      body: [switcher, settingsEl, composeEl],
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }),
        ui.button({ label: editing ? "Save and apply" : "Install", kind: "primary", onClick: submit })],
    });
    if (firstError) showError(firstError);
  }

  await load();
  return () => { alive = false; clearInterval(timer); unregister(); };
}
