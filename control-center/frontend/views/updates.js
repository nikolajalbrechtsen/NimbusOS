// =============================================================================
// views/updates.js: the Updates page
// =============================================================================
// What: - banners: restart required (with "Restart now"), this system no longer
//         receiving NimbusOS software (nimbusos-desktop removed, NOTE-48), one
//         per repository problem with its fix (DNS, TLS, signature, expired;
//         a bad signature is never shown as "no updates"), release-note
//         problems, a newer NimbusOS release;
//       - "This system": version, channel, policy, last check;
//       - release notes of the newest release for this channel;
//       - channel (Stable / Beta; Beta warns first) and policy (Automatic /
//         Notify only / Off) as radio cards;
//       - pending updates grouped Security / NimbusOS / Debian;
//       - "Install updates": streams the log into the drawer; if the window or
//         the backend restarts meanwhile (an update may restart it), it
//         reconnects and shows the run again;
//       - history of past apt runs.
// Why:  spec section 4.4 (Updates page).
// =============================================================================

// The install log keeps streaming into the (global) drawer while the user
// visits other pages; this makes sure one run is followed only once.
let following = null;

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const { h, clear } = ui;
  let alive = true;

  const checkBtn = ui.button({ label: "Check now", iconName: "restart", onClick: checkNow });
  const installBtn = ui.button({ label: "Install updates", kind: "primary", iconName: "download", onClick: install });
  const body = h("div", { class: "stack" }, h("div", { class: "loading", text: "Loading update status…" }));
  root.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: "Updates" }),
        h("p", { class: "desc", text: "NimbusOS updates come from the NimbusOS repository, everything else from Debian." })),
      h("div", { class: "actions" }, checkBtn, installBtn)),
    body);

  // --- data ----------------------------------------------------------------
  async function load() {
    try {
      draw(await api.get("/api/updates"));
    } catch (error) {
      clear(body, ui.banner("bad", "The update status could not be read", ui.errorText(error)));
    }
  }

  async function checkNow() {
    try {
      const status = await api.post("/api/updates/check");
      draw(status);
      const n = total(status);
      if (status.ok) ui.toast(n ? `${n} update${n === 1 ? "" : "s"} available.` : "Everything is up to date.", "good");
      else ui.toast("The check found problems; see the messages at the top.", "warn");
      ctx.refreshSummary();
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  const total = (status) => Object.values(status.counts || {}).reduce((a, b) => a + Number(b || 0), 0);

  // --- install ------------------------------------------------------------------
  async function install() {
    try {
      const { job } = await api.post("/api/updates/install");
      follow(job);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  function follow(job) {
    if (following === job) return;
    following = job;
    const drawer = ui.logDrawer("Installing updates");
    let finished = false;
    let attempts = 0;
    const connect = () => {
      api.socket(`/ws/updates/${job}`, {
        onOpen: () => { attempts = 0; drawer.status("info", "Running"); },
        onMessage: (message) => {
          if (message.type === "log") drawer.log(message.line);
          else if (message.type === "done") {
            finished = true;
            following = null;
            drawer.done(message.ok, message.ok ? "Installed" : "Failed");
            ui.toast(message.ok ? "Updates installed." : "Installing updates failed. The log says why.", message.ok ? "good" : "bad");
            ctx.refreshSummary();
            if (alive) load();
          } else if (message.type === "error") {
            finished = true;
            following = null;
            drawer.log(message.message);
            drawer.done(false, "Failed");
          }
        },
        onClose: () => {
          if (finished) return;
          // The run continues on the system; an update may be restarting this
          // service. Reconnect and show the whole log again.
          attempts += 1;
          if (attempts > 40) {
            following = null;
            drawer.done(false, "Disconnected");
            drawer.log("Lost the connection. The update keeps running; open the Updates page again to see its result.");
            return;
          }
          drawer.status("warn", "Reconnecting…");
          setTimeout(() => {
            drawer.log("--- reconnected; the log so far: ---");
            connect();
          }, Math.min(1000 * 2 ** Math.min(attempts, 3), 8000));
        },
      });
    };
    connect();
  }

  // --- drawing ------------------------------------------------------------------------
  function draw(status) {
    const installed = status.installed || {};
    const n = total(status);
    installBtn.disabled = n === 0;
    installBtn.title = n === 0 ? "Everything is up to date." : "";
    clear(body, ...banners(status), h("div", { class: "grid-2" }, systemPanel(status, installed), notesPanel(status)),
      channelPanel(status), policyPanel(status), pendingPanel(status), historyPanel());
  }

  function banners(status) {
    const list = [];
    if (status.reboot_required) {
      const pkgs = (status.reboot_packages || []).join(", ");
      list.push(ui.banner("warn", "Restart required",
        `Installed updates take effect after a restart${pkgs ? ` (${pkgs})` : ""}.`,
        ui.button({ label: "Restart now", iconName: "power", onClick: restart })));
    }
    if (status.desktop_metapackage_installed === false) {
      list.push(ui.banner("warn", "This system no longer receives new NimbusOS software.",
        "Reinstall nimbusos-desktop: sudo apt install nimbusos-desktop"));
    }
    const sources = { nimbusos: "NimbusOS repository", debian: "Debian repository", docker: "Docker repository" };
    const kinds = { signature: "could not be verified", expired: "has expired", tls: "secure connection failed",
      dns: "name could not be resolved", unreachable: "could not be reached", other: "reported a problem" };
    for (const error of status.errors || []) {
      list.push(ui.banner("bad", `${sources[error.source] || "A repository"}: ${kinds[error.kind] || kinds.other}`, error.hint));
    }
    if (status.releases_error) list.push(ui.banner("info", "Release notes unavailable", status.releases_error));
    if (status.update_available && status.latest) {
      list.push(ui.banner("info", `${installedOf(status).name || "NimbusOS"} ${status.latest.version} is available`,
        "Install updates to get it. The release notes are below."));
    }
    return list;
  }
  const installedOf = (status) => status.installed || {};

  function systemPanel(status, inst) {
    const policies = { automatic: "Automatic", notify: "Notify only", off: "Off" };
    const checked = h("span", { text: ui.timeAgo(status.checked_at), title: status.checked_at ? ui.dateTime(status.checked_at) : "" });
    return ui.panel("This system", {},
      h("dl", { class: "facts" },
        h("dt", { text: "Version" }), h("dd", { class: "mono", text: inst.version || "unknown" }),
        h("dt", { text: "Channel" }), h("dd", {}, ui.tag(status.channel === "beta" ? "warn" : "accent", status.channel === "beta" ? "Beta" : "Stable")),
        h("dt", { text: "Updates" }), h("dd", { text: policies[status.policy] || status.policy || "" }),
        h("dt", { text: "Last check" }), h("dd", {}, checked),
        h("dt", { text: "Package version" }), h("dd", { class: "mono dim", text: inst.deb_version || "" })));
  }

  function notesPanel(status) {
    const latest = status.latest;
    if (!latest) {
      return ui.panel("Release notes", {}, ui.emptyState({ iconName: "info", title: "No release notes yet",
        text: "They appear after the first successful check." }));
    }
    return ui.panel("Release notes", {},
      h("div", { class: "stack" },
        h("div", {}, h("strong", { text: `${installedOf(status).name || "NimbusOS"} ${latest.version}` }), " ",
          ui.tag(latest.channel === "beta" ? "warn" : "accent", latest.channel === "beta" ? "Beta" : "Stable"), " ",
          latest.restart_required ? ui.tag("warn", "Restart needed") : null,
          latest.date ? h("div", { class: "dim", text: ui.dateTime(latest.date) }) : null),
        h("div", { class: "notes", text: latest.notes || "No notes were published for this release." })));
  }

  function channelPanel(status) {
    return ui.panel("Update channel", {},
      ui.radioCards({
        label: "Update channel",
        value: status.channel,
        options: [
          { value: "stable", title: "Stable", desc: "Tested releases. Recommended for every machine." },
          { value: "beta", title: "Beta", desc: "Early versions of the next release, to try new features first." },
        ],
        onChange: async (channel) => {
          if (channel === "beta") {
            const ok = await ui.confirmDialog({
              title: "Switch to Beta?",
              message: "Beta versions are not fully tested and may break things on this machine. Switching back to Stable later doesn't downgrade: you stay on the beta version until a newer stable release comes out.",
              confirmLabel: "Switch to Beta",
            });
            if (!ok) return false;
          }
          try {
            draw(await api.put("/api/updates/channel", { channel }));
            ui.toast(channel === "beta" ? "Beta channel selected." : "Stable channel selected.", "good");
            ctx.refreshSummary();
            return true;
          } catch (error) {
            ui.toast(ui.errorText(error), "bad");
            return false;
          }
        },
      }));
  }

  function policyPanel(status) {
    return ui.panel("Automatic updates", {},
      h("p", { class: "muted", text: "Debian security updates always install automatically. This setting is for NimbusOS updates. The computer never restarts by itself." }),
      h("div", { style: { marginTop: "12px" } },
        ui.radioCards({
          label: "Update policy",
          value: status.policy,
          options: [
            { value: "automatic", title: "Automatic", desc: "Installs NimbusOS updates once a day." },
            { value: "notify", title: "Notify only", desc: "Checks daily and shows them here; you install them." },
            { value: "off", title: "Off", desc: "No automatic checks. “Check now” still works." },
          ],
          onChange: async (policy) => {
            try {
              draw(await api.put("/api/updates/policy", { policy }));
              ui.toast("Update policy saved.", "good");
              return true;
            } catch (error) {
              ui.toast(ui.errorText(error), "bad");
              return false;
            }
          },
        })));
  }

  function pendingPanel(status) {
    const groups = [
      ["security", "Security", "bad"],
      ["nimbusos", "NimbusOS", "accent"],
      ["debian", "Debian", ""],
    ];
    const rows = [];
    for (const [key, title, kind] of groups) {
      const items = (status.pending || {})[key] || [];
      if (!items.length) continue;
      rows.push(h("tr", { class: "group-row" }, h("td", { colspan: "4" }, ui.tag(kind, title), " ",
        h("span", { class: "dim", text: `${items.length} package${items.length === 1 ? "" : "s"}` }))));
      for (const item of items) {
        rows.push(h("tr", {},
          h("td", { class: "mono", text: item.name }),
          h("td", { class: "mono dim col-secondary", text: item.from || "new" }),
          h("td", { class: "mono", text: item.to }),
          h("td", { class: "dim col-secondary", text: item.origin || "" })));
      }
    }
    if (!rows.length) {
      return ui.panel("Pending updates", {}, ui.emptyState({ iconName: "check", title: "Everything is up to date",
        text: status.checked_at ? `Last checked ${ui.timeAgo(status.checked_at)}.` : "No check has run yet. Use “Check now”." }));
    }
    return ui.panel("Pending updates", { flush: true },
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", { text: "Package" }), h("th", { class: "col-secondary", text: "Installed" }),
          h("th", { text: "New" }), h("th", { class: "col-secondary", text: "From" }))),
        h("tbody", {}, rows))));
  }

  function historyPanel() {
    const host = h("div", {}, h("div", { class: "loading", text: "Loading history…" }));
    api.get("/api/updates/history?limit=20").then(({ entries }) => {
      if (!entries.length) {
        clear(host, ui.emptyState({ iconName: "history", title: "No history yet", text: "Every apt run is listed here." }));
        return;
      }
      const summary = (changes) => Object.entries(changes).map(([action, items]) => `${items.length} ${action}`).join(", ") || "no changes";
      clear(host, h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, h("th", { text: "When" }), h("th", { text: "Changes" }), h("th", { class: "col-secondary", text: "Command" }))),
        h("tbody", {}, entries.map((e) => h("tr", {},
          h("td", { class: "num", text: e.start }),
          h("td", {}, e.error ? ui.tag("bad", "Error") : null, e.error ? " " : null, summary(e.changes)),
          h("td", { class: "mono dim col-secondary", text: e.command })))))));
    }).catch((error) => clear(host, ui.banner("bad", "The history could not be read", ui.errorText(error))));
    return ui.panel("History", { flush: true }, host);
  }

  async function restart() {
    const ok = await ui.confirmDialog({
      title: "Restart now?", confirmLabel: "Restart now", danger: true,
      message: "Apps, virtual machines and network shares stop until the computer is back, usually within a minute or two.",
    });
    if (!ok) return;
    try {
      await api.post("/api/system/reboot");
      ui.toast("Restarting…", "info", 20000);
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
  }

  // --- start ----------------------------------------------------------------------------
  await load();
  try {
    const { job } = await api.get("/api/updates/job");
    if (job) follow(job);
  } catch { /* nothing running */ }
  // Leaving the page keeps a running install's log streaming into the drawer.
  return () => { alive = false; };
}
