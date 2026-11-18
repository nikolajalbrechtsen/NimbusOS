// =============================================================================
// views/vms.js: the Virtual Machines page
// =============================================================================
// What: - Machines: one row per VM with its state (Running, Paused with the
//         reason, Shutting down, Stopped, Crashed), live CPU (% of its own
//         vCPUs) and memory, its vCPUs, memory and disk, and actions:
//         Console (the VM's screen in the page, or popped out to its own
//         window), Start, Shut down (ACPI), Restart, Force off, Options (CD
//         drive, start with the computer) and Delete (its disk only when
//         ticked and the name typed). Refreshes every 3 seconds;
//       - ISO library: the installer images with their size and the VMs
//         using them; upload in 8 MiB chunks with progress (closing the
//         drawer cancels), delete;
//       - Create VM: name, Linux or Windows, installer ISO (and the virtio
//         driver ISO for Windows), vCPUs, memory, disk size, UEFI or legacy
//         BIOS, NAT or a bridge, start now, start with the computer; the
//         field an error is about is highlighted. Without hardware
//         virtualization a banner says VMs will be slow and how to fix it.
// Why:  spec Tab B; docs/PLAN.md NOTE-67.
// =============================================================================

import { openConsole } from "../lib/vnc.js";

const STATES = {
  running: ["good", "Running"], paused: ["warn", "Paused"], stopping: ["info", "Shutting down"],
  stopped: ["", "Stopped"], crashed: ["bad", "Crashed"], suspended: ["warn", "Suspended"], unknown: ["warn", "Unknown"],
};
const PRESETS = { linux: "Linux", windows: "Windows" };
const GIB = 1024 ** 3;

// Memory and disks in binary units (GiB), as VM settings are given.
function size(n, digits = 1) {
  if (n === null || n === undefined) return "–";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  let v = Number(n);
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${Number.isInteger(v) ? v : v.toFixed(digits)} ${units[i]}`;
}

export async function render(root, ctx) {
  const { api, ui } = ctx;
  const { h, clear } = ui;
  let tab = "machines";
  let data = { vms: [], host: null, libvirt: { ok: true } };
  let library = { isos: [], path: "", max_bytes: 0, free_bytes: null };
  let alive = true;
  let afterUpload = null;

  const fileInput = h("input", { type: "file", accept: ".iso,.img", hidden: true, "aria-hidden": "true", tabindex: "-1" });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (file) upload(file);
  });
  const pickIso = (then = null) => { afterUpload = then; fileInput.click(); };

  const body = h("div", { class: "stack" }, h("div", { class: "loading", text: "Loading virtual machines…" }));
  const tabs = ui.segmented({
    label: "Virtual machines", value: tab,
    options: [{ value: "machines", title: "Machines" }, { value: "isos", title: "ISO library" }],
    onChange: (value) => { tab = value; draw(); },
  });
  root.append(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: "Virtual Machines" }),
        h("p", { class: "desc", text: "KVM/QEMU virtual machines, with their screen right here." })),
      h("div", { class: "actions" },
        ui.button({ label: "Upload ISO", iconName: "upload", onClick: () => pickIso() }),
        ui.button({ label: "Create VM", kind: "primary", iconName: "vms", onClick: () => createVm() }))),
    h("div", { style: { marginBottom: "16px" } }, tabs),
    body, fileInput);

  // --- data --------------------------------------------------------------------------
  async function load() {
    try {
      [data, library] = await Promise.all([api.get("/api/vms"), api.get("/api/isos")]);
      if (alive) draw();
    } catch (error) {
      clear(body, ui.banner("bad", "The virtual machines could not be read", ui.errorText(error)));
    }
  }
  async function refresh() {
    try {
      data = await api.get("/api/vms");
      if (alive && tab === "machines" && !document.querySelector(".backdrop")) draw();
    } catch { /* keep the last view */ }
  }
  const timer = setInterval(refresh, 3000);
  const unregister = ctx.registerSearch(async (q) => data.vms
    .filter((vm) => !q || vm.name.toLowerCase().includes(q))
    .map((vm) => ({ title: vm.name, sub: `VM · ${(STATES[vm.state] || STATES.unknown)[1]}`, icon: "vms",
      action: () => { ctx.navigate("vms"); if (vm.console) consoleFor(vm); } })));

  function draw() {
    clear(body, ...(tab === "machines" ? machines() : isoView()));
  }

  // --- machines ----------------------------------------------------------------------------
  function hostBanners() {
    if (!data.libvirt.ok) return [ui.banner("bad", "libvirt is not available", data.libvirt.message)];
    if (data.host && !data.host.kvm) {
      return [ui.banner("warn", "Hardware virtualization is off",
        "VMs still run, but very slowly. Turn on Intel VT-x or AMD-V (SVM) in this computer's firmware (BIOS/UEFI) settings, then restart.")];
    }
    return [];
  }

  function usage(vm) {
    if (vm.cpu_percent === null || vm.cpu_percent === undefined) return h("span", { class: "dim", text: "–" });
    const pct = Math.round(vm.cpu_percent);
    const fill = h("i", { class: "vm", style: { width: `${Math.min(100, pct)}%` } });
    return h("div", { class: "vm-usage" },
      h("div", { class: "line" }, h("span", { class: "dim", text: "CPU" }), h("div", { class: "bar thin" }, fill),
        h("span", { class: "num", text: `${pct} %` })),
      h("div", { class: "line" }, h("span", { class: "dim", text: "RAM" }),
        h("span", { class: "num", text: `${size(vm.memory_used)} of ${size(vm.memory)}` })));
  }

  function machines() {
    const out = hostBanners();
    if (!data.libvirt.ok) return out;
    if (!data.vms.length) {
      out.push(ui.panel(null, {}, ui.emptyState({
        iconName: "vms", title: "No virtual machines yet",
        text: library.isos.length ? "Create a VM from one of the ISOs in your library."
          : "Upload an installer ISO, then create a VM from it.",
        action: ui.button({ label: "+ Create VM", kind: "primary", onClick: () => createVm() }),
      })));
      return out;
    }
    const rows = data.vms.map((vm) => {
      const [kind, label] = STATES[vm.state] || STATES.unknown;
      const on = ["running", "paused", "stopping"].includes(vm.state);
      const actions = [];
      if (vm.console) actions.push(ui.button({ label: "Console", small: true, iconName: "vms", onClick: () => consoleFor(vm) }));
      if (vm.state === "paused") actions.push(ui.button({ label: "Resume", small: true, iconName: "play", onClick: () => act(vm, "resume") }));
      if (!on) actions.push(ui.button({ label: "Start", small: true, iconName: "play", onClick: () => act(vm, "start") }));
      if (on) {
        actions.push(
          ui.button({ small: true, kind: "ghost", iconName: "power", title: "Shut down (asks the VM's system)", onClick: () => act(vm, "shutdown") }),
          ui.button({ small: true, kind: "ghost", iconName: "restart", title: "Restart", onClick: () => act(vm, "reboot") }),
          ui.button({ small: true, kind: "ghost", iconName: "stop", title: "Force off (like pulling the plug)", onClick: () => forceOff(vm) }));
      }
      actions.push(
        ui.button({ small: true, kind: "ghost", iconName: "settings", title: "Options: CD drive, start with the computer", onClick: () => options(vm) }),
        ui.button({ small: true, kind: "ghost", iconName: "x", title: "Delete", onClick: () => deleteVm(vm) }));
      const disk = vm.disks.reduce((sum, d) => sum + (d.capacity || 0), 0);
      return h("tr", {},
        h("td", {}, h("div", { class: "app-name" },
          h("span", { class: `app-icon letter vm-${vm.preset || "other"}`, "aria-hidden": "true", text: (PRESETS[vm.preset] || vm.name).charAt(0) }),
          h("div", { class: "names" }, h("strong", { text: vm.name, title: vm.name }),
            h("span", { text: `${PRESETS[vm.preset] || "Other"} · ${vm.firmware === "uefi" ? "UEFI" : "BIOS"}` })))),
        h("td", {}, h("div", { class: "status-cell" }, ui.pill(kind, label),
          vm.reason ? h("span", { class: "reason", text: vm.reason }) : null,
          vm.autostart ? h("span", { class: "reason", text: "Starts with the computer" }) : null)),
        h("td", {}, usage(vm)),
        h("td", { class: "col-wide" }, h("div", { class: "vm-spec" },
          h("span", { class: "num", text: `${vm.vcpus} vCPU · ${size(vm.memory, 0)} · ${disk ? size(disk, 0) : "no disk"}` }),
          vm.iso ? h("span", { class: "dim mono", title: vm.iso_missing ? "This file is no longer in the ISO library" : null,
            text: `CD: ${vm.iso}${vm.iso_missing ? " (missing)" : ""}` }) : null)),
        h("td", { class: "actions" }, h("div", { class: "row-actions" }, actions)));
    });
    out.push(ui.panel(null, { flush: true }, h("div", { class: "table-wrap" }, h("table", { class: "table vm-table" },
      h("thead", {}, h("tr", {}, h("th", { text: "VM" }), h("th", { text: "Status" }), h("th", { text: "Usage" }),
        h("th", { class: "col-wide", text: "Resources" }), h("th", { class: "actions", text: "" }))),
      h("tbody", {}, rows)))));
    return out;
  }

  async function act(vm, action) {
    try {
      const { message } = await api.post(`/api/vms/${encodeURIComponent(vm.name)}/actions/${action}`);
      ui.toast(message, "good");
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
    await refresh();
    ctx.refreshSummary();
  }

  async function forceOff(vm) {
    const ok = await ui.confirmDialog({ title: `Force off ${vm.name}?`, danger: true, confirmLabel: "Force off",
      message: "This is like pulling the power plug: anything not saved inside the VM is lost. Try Shut down first." });
    if (ok) await act(vm, "force-off");
  }

  // --- console -----------------------------------------------------------------------------
  function consoleFor(vm) {
    const screen = h("div", { class: "vnc-screen", tabindex: "-1" });
    let rfb = null;
    const m = ui.modal({
      title: `Console: ${vm.name}`, wide: true, body: screen, hint: "Connecting…",
      actions: [
        ui.button({ label: "Ctrl+Alt+Del", small: true, onClick: () => rfb && rfb.sendCtrlAltDel() }),
        ui.button({ small: true, kind: "ghost", iconName: "fullscreen", title: "Full screen", onClick: () => screen.requestFullscreen && screen.requestFullscreen() }),
        ui.button({ small: true, kind: "ghost", iconName: "external", title: "Open in its own window", onClick: () => {
          m.close();
          // Not "noopener": the new window takes this window's session with it.
          window.open(`/console.html#${encodeURIComponent(vm.name)}`, `nimbus-console-${vm.uuid}`, "popup,width=1100,height=800");
        } }),
        ui.button({ label: "Close", small: true, onClick: () => m.close() })],
      onClose: () => rfb && rfb.disconnect(),
    });
    m.el.classList.add("console-modal");
    openConsole(api, vm.name, screen, (kind, text) => m.setHint(text))
      .then((r) => { rfb = r; })
      .catch((error) => m.setHint(ui.errorText(error)));
  }

  // --- options and delete -------------------------------------------------------------------
  function isoOptions(emptyLabel) {
    return [{ value: "", label: emptyLabel }, ...library.isos.map((i) => ({ value: i.name, label: i.name }))];
  }

  function options(vm) {
    let iso = vm.iso_missing ? "" : (vm.iso || "");
    let autostart = vm.autostart;
    const m = ui.modal({
      title: `Options: ${vm.name}`, body: [
        ui.field("CD drive", ui.select(isoOptions("Empty (eject the CD)"), iso, { onChange: (v) => { iso = v; } }),
          { key: "iso", hint: vm.state === "running" ? "Changes the CD in the running VM too." : "Used from the next start." }),
        ui.checkbox("Start when the computer starts", autostart, (v) => { autostart = v; })],
      actions: [ui.button({ label: "Cancel", onClick: () => m.close() }),
        ui.button({ label: "Save", kind: "primary", onClick: async () => {
          try {
            const messages = [];
            if (iso !== (vm.iso_missing ? "" : (vm.iso || ""))) {
              messages.push((await api.put(`/api/vms/${encodeURIComponent(vm.name)}/media`, { iso: iso || null })).message);
            }
            if (autostart !== vm.autostart) {
              messages.push((await api.put(`/api/vms/${encodeURIComponent(vm.name)}/autostart`, { enabled: autostart })).message);
            }
            m.close();
            if (messages.length) ui.toast(messages.join(" "), "good");
            await load();
          } catch (error) {
            if (!ui.markInvalid(m.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
          }
        } })],
    });
  }

  async function deleteVm(vm) {
    let deleteDisks = false;
    const own = vm.disks.filter((d) => d.in_pool);
    const details = own.length ? h("div", { class: "stack" },
      ui.checkbox(`Also delete its disk (${own.map((d) => d.path).join(", ")})`, false, (v) => { deleteDisks = v; }),
      h("p", { class: "muted", text: "Without the tick, the disk stays, and a new VM can't use it by accident: new disks always get a new name." }))
      : null;
    const first = await ui.confirmDialog({
      title: `Delete ${vm.name}?`, danger: true, confirmLabel: "Continue", details,
      message: `${vm.state === "stopped" ? "The VM" : "The VM is turned off, then it"} is removed with its UEFI settings and TPM.`,
    });
    if (!first) return;
    if (deleteDisks) {
      const sure = await ui.confirmDialog({ title: `Delete ${vm.name}'s disk?`, danger: true, confirmLabel: "Delete VM and disk",
        message: "Everything installed in the VM is deleted for good.", typeToConfirm: vm.name });
      if (!sure) return;
    }
    try {
      const { message } = await api.del(`/api/vms/${encodeURIComponent(vm.name)}?delete_disks=${deleteDisks}`);
      ui.toast(message, "good");
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
    await load();
    ctx.refreshSummary();
  }

  // --- ISO library ----------------------------------------------------------------------------
  function isoView() {
    const out = [];
    const copy = library.path ? ui.copyButton(library.path, { label: "Copy path" }) : null;
    const head = h("p", { class: "muted iso-path" }, "Installer images live in ", h("span", { class: "mono", text: library.path }),
      ". When an “isos” share exists, files copied into it over the network show up here too.", copy);
    if (!library.isos.length) {
      out.push(ui.panel(null, {}, ui.emptyState({
        iconName: "disc", title: "No ISO files yet",
        text: "Upload an installer ISO (Debian, Ubuntu, Windows…) to create VMs from it.",
        action: ui.button({ label: "Upload ISO", kind: "primary", iconName: "upload", onClick: () => pickIso() }),
      })), head);
      return out;
    }
    const rows = library.isos.map((iso) => h("tr", {},
      h("td", {}, h("span", { class: "mono", text: iso.name })),
      h("td", { class: "num", text: size(iso.size) }),
      h("td", { class: "col-secondary", text: ui.timeAgo(new Date(iso.modified * 1000).toISOString()) }),
      h("td", {}, iso.used_by.length ? iso.used_by.map((n) => ui.tag("violet", n)) : h("span", { class: "dim", text: "–" })),
      h("td", { class: "actions" }, ui.button({ small: true, kind: "ghost", iconName: "x", title: `Delete ${iso.name}`,
        onClick: () => deleteIso(iso) }))));
    out.push(ui.panel(null, { flush: true }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, h("th", { text: "File" }), h("th", { text: "Size" }), h("th", { class: "col-secondary", text: "Added" }),
        h("th", { text: "In the CD drive of" }), h("th", { class: "actions", text: "" }))),
      h("tbody", {}, rows)))), head);
    if (library.free_bytes !== null) out.push(h("p", { class: "dim", text: `${size(library.free_bytes)} free. Uploads up to ${size(library.max_bytes, 0)} each.` }));
    return out;
  }

  async function deleteIso(iso) {
    if (iso.used_by.length) {
      ui.toast(`'${iso.name}' is in the CD drive of ${iso.used_by.join(", ")}. Eject it there first (Options).`, "warn");
      return;
    }
    const ok = await ui.confirmDialog({ title: `Delete ${iso.name}?`, danger: true, confirmLabel: "Delete",
      message: `The file is removed from ${library.path}.` });
    if (!ok) return;
    try {
      ui.toast((await api.del(`/api/isos/${encodeURIComponent(iso.name)}`)).message, "good");
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
    }
    await load();
  }

  function isoName(file) {
    let name = file.name.replace(/[^A-Za-z0-9 ._+()-]/g, "_");
    if (!/^[A-Za-z0-9]/.test(name)) name = `iso-${name}`;
    return name;
  }

  async function upload(file) {
    if (!/\.(iso|img)$/i.test(file.name)) {
      ui.toast("Choose an .iso or .img file.", "bad");
      return;
    }
    const name = isoName(file);
    let job;
    try {
      job = await api.post("/api/isos/uploads", { name, size: file.size });
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
      return;
    }
    let cancelled = false;
    let finished = false;
    const drawer = ui.logDrawer(`Uploading ${name}`, { onClose: () => { if (!finished) cancelled = true; } });
    drawer.progress(0);
    drawer.log(`${file.name} → ${library.path || "the ISO library"}/${name} (${size(file.size)})`);
    const started = performance.now();
    let offset = 0;
    let nextLog = 0.1;
    try {
      while (offset < file.size) {
        if (cancelled) throw new Error("cancelled");
        const chunk = file.slice(offset, offset + job.chunk_bytes);
        try {
          await api.putBlob(`/api/isos/uploads/${job.id}?offset=${offset}`, chunk);
        } catch (error) {
          if (error.status !== 0) throw error;
          await api.putBlob(`/api/isos/uploads/${job.id}?offset=${offset}`, chunk);     // one retry
        }
        offset += chunk.size;
        drawer.progress(offset / file.size);
        if (offset / file.size >= nextLog || offset === file.size) {
          const rate = offset / Math.max(0.001, (performance.now() - started) / 1000);
          drawer.log(`${size(offset)} of ${size(file.size)} (${size(rate)}/s)`);
          nextLog += 0.1;
        }
      }
      const result = await api.post(`/api/isos/uploads/${job.id}/finish`);
      finished = true;
      drawer.done(true, "Uploaded");
      ui.toast(result.message, "good");
      await load();
      if (afterUpload) afterUpload(result.name);
    } catch (error) {
      finished = true;
      try { await api.del(`/api/isos/uploads/${job.id}`); } catch { /* already gone */ }
      if (cancelled) {
        ui.toast(`The upload of ${name} was cancelled.`, "info");
      } else {
        drawer.log(ui.errorText(error));
        drawer.done(false, "Failed");
        ui.toast(ui.errorText(error), "bad");
      }
    } finally {
      afterUpload = null;
    }
  }

  // --- create ---------------------------------------------------------------------------------
  async function createVm() {
    let opts;
    try {
      opts = await api.get("/api/vms/options");
    } catch (error) {
      ui.toast(ui.errorText(error), "bad");
      return;
    }
    const host = opts.host;
    // A first guess at the ISOs from their names; anything the user picks wins.
    const virtio = (n) => /virtio/i.test(n);
    const windowsIso = (n) => /win/i.test(n) && !virtio(n);
    const guess = (preset) => (preset === "windows" ? opts.isos.find(windowsIso)
      : opts.isos.find((n) => !windowsIso(n) && !virtio(n))) || "";
    const form = { name: "", preset: "linux", ...opts.defaults.linux, memory_gib: opts.defaults.linux.memory_mib / 1024,
      network: { mode: "nat" }, iso: guess("linux"), drivers_iso: "", start: true, autostart: false };
    if (!host.efi) form.firmware = "bios";
    const edited = new Set();
    const numberInput = (key, attrs) => ui.input({ value: form[key], type: "number", ...attrs,
      onInput: (v) => { form[key] = Number(v); edited.add(key); } });

    const isoSelect = ui.select(isoOptions(opts.isos.length ? "No ISO (empty CD drive)" : "No ISO yet"), form.iso,
      { label: "Installer ISO", onChange: (v) => { form.iso = v; edited.add("iso"); } });
    const driverSelect = ui.select(isoOptions("None (SATA and e1000e: slower, no drivers needed)"), "",
      { label: "Driver ISO", onChange: (v) => { form.drivers_iso = v; edited.add("drivers_iso"); } });
    const driverField = ui.field("Driver ISO (virtio-win)", driverSelect, { key: "drivers_iso",
      hint: "With the virtio-win ISO, Windows uses the faster virtio disk and network: in Setup choose “Load driver”." });
    driverField.hidden = true;
    const vcpus = numberInput("vcpus", { min: 1, max: host.threads, label: "vCPUs" });
    const memory = numberInput("memory_gib", { min: 0.25, max: Math.floor(host.memory / GIB), label: "Memory in GiB" });
    memory.step = "0.25";
    const disk = numberInput("disk_gib", { min: 1, label: "Disk size in GiB" });
    const pathHint = () => `Disk: ${opts.images_dir}/${form.name || "<name>"}.qcow2`;

    const bridgeSelect = ui.select(opts.bridges.map((b) => ({ value: b, label: b })), opts.bridges[0] || "",
      { label: "Bridge", onChange: (v) => { form.network = { mode: "bridge", bridge: v }; } });
    const bridgeField = ui.field("Bridge", bridgeSelect, { key: "network.bridge" });
    bridgeField.hidden = true;

    const presetCards = ui.radioCards({
      label: "Operating system", value: form.preset,
      options: [
        { value: "linux", title: "Linux", desc: "virtio disk, network and graphics; UEFI without Secure Boot." },
        { value: "windows", title: "Windows", desc: "Secure Boot, TPM 2.0 and Hyper-V enlightenments, as Windows 11 needs." },
      ],
      onChange: (v) => {
        form.preset = v;
        const d = opts.defaults[v];
        if (!edited.has("vcpus")) { form.vcpus = Math.min(d.vcpus, host.threads); vcpus.value = form.vcpus; }
        if (!edited.has("memory_gib")) { form.memory_gib = Math.min(d.memory_mib / 1024, Math.floor(host.memory / GIB)); memory.value = form.memory_gib; }
        if (!edited.has("disk_gib")) { form.disk_gib = d.disk_gib; disk.value = form.disk_gib; }
        if (!edited.has("iso")) { form.iso = guess(v); isoSelect.value = form.iso; }
        if (!edited.has("drivers_iso")) {
          form.drivers_iso = v === "windows" ? (opts.isos.find(virtio) || "") : "";
          driverSelect.value = form.drivers_iso;
        }
        driverField.hidden = v !== "windows";
        if (v !== "windows") form.drivers_iso = "";
        return true;
      },
    });
    const firmwareCards = ui.radioCards({
      label: "Firmware", value: form.firmware,
      options: [
        { value: "uefi", title: "UEFI (recommended)", desc: host.efi ? "Modern firmware; Windows 11 needs it." : "Not available: the ovmf package is missing.", disabled: !host.efi },
        { value: "bios", title: "Legacy BIOS", desc: "For older systems that can't start with UEFI." },
      ],
      onChange: (v) => { form.firmware = v; return true; },
    });
    const networkCards = ui.radioCards({
      label: "Network", value: "nat",
      options: [
        { value: "nat", title: "NAT", desc: "A private network behind this computer. The VM reaches the internet and your network." },
        { value: "bridge", title: "Bridge", desc: opts.bridges.length ? "The VM gets its own address on your network, like another computer."
          : "No network bridge is set up on this computer.", disabled: !opts.bridges.length },
      ],
      onChange: (v) => {
        form.network = v === "bridge" ? { mode: "bridge", bridge: bridgeSelect.value } : { mode: "nat" };
        bridgeField.hidden = v !== "bridge";
        return true;
      },
    });
    const nameInput = ui.input({ mono: true, placeholder: "for example debian-server", label: "Name",
      onInput: (v) => { form.name = v.trim(); dialog.setHint(pathHint()); } });

    const dialog = ui.modal({
      title: "Create a virtual machine", wide: true, hint: pathHint(),
      body: [
        ...(!host.kvm ? [ui.banner("warn", "Hardware virtualization is off", "The VM will run, but very slowly, until VT-x or AMD-V is turned on in the firmware settings.")] : []),
        ui.field("Name", nameInput, { key: "name", hint: "Letters, digits, '.', '-' and '_'." }),
        h("div", { class: "field", "data-field": "preset" }, h("span", { text: "Operating system" }), presetCards),
        h("div", { class: "grid-2" },
          ui.field("Installer ISO", h("div", { class: "inline" }, isoSelect,
            ui.button({ label: "Upload…", small: true, iconName: "upload", onClick: () => pickIso((name) => {
              isoSelect.append(h("option", { value: name, text: name }));
              isoSelect.value = name;
              form.iso = name;
            }) })), { key: "iso", hint: "From the ISO library. You can change the CD later in Options." }),
          driverField),
        h("div", { class: "grid-3" },
          ui.field("vCPUs", vcpus, { key: "vcpus", hint: `This computer has ${host.threads}.` }),
          ui.field("Memory (GiB)", memory, { key: "memory_mib", hint: `Of ${size(host.memory, 0)}.` }),
          ui.field("Disk (GiB)", disk, { key: "disk_gib",
            hint: opts.free_bytes !== null ? `Grows as it fills; ${size(opts.free_bytes, 0)} free.` : "Grows as it fills." })),
        h("div", { class: "field", "data-field": "firmware" }, h("span", { text: "Firmware" }), firmwareCards),
        h("div", { class: "field", "data-field": "network" }, h("span", { text: "Network" }), networkCards),
        bridgeField,
        h("div", { class: "inline wrap" },
          ui.checkbox("Start it now", form.start, (v) => { form.start = v; }),
          ui.checkbox("Start when the computer starts", form.autostart, (v) => { form.autostart = v; }))],
      actions: [ui.button({ label: "Cancel", onClick: () => dialog.close() }),
        ui.button({ label: "Create", kind: "primary", onClick: submit })],
    });
    nameInput.focus();

    async function submit() {
      ui.clearInvalid(dialog.el);
      const payload = { name: form.name, preset: form.preset, vcpus: form.vcpus, memory_mib: Math.round(form.memory_gib * 1024),
        disk_gib: form.disk_gib, firmware: form.firmware, network: form.network, iso: form.iso || null,
        drivers_iso: form.preset === "windows" ? (form.drivers_iso || null) : null, start: form.start, autostart: form.autostart };
      try {
        const result = await api.post("/api/vms", payload);
        dialog.close();
        ui.toast(result.message, result.started || !form.start ? "good" : "warn");
        tab = "machines";
        tabs.querySelectorAll("button")[0].click();
        await load();
        ctx.refreshSummary();
      } catch (error) {
        if (!ui.markInvalid(dialog.el, error.field, ui.errorText(error))) ui.toast(ui.errorText(error), "bad");
      }
    }
  }

  await load();
  return () => { alive = false; clearInterval(timer); unregister(); };
}
