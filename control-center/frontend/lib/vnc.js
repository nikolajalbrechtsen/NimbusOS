// =============================================================================
// lib/vnc.js: a VM's screen in the page (noVNC through websockify)
// =============================================================================
// What: openConsole(api, name, target, onStatus) asks the backend for a
//       console (it writes a one-time websockify token for this VM and
//       returns the VM's VNC password), loads the packaged noVNC from
//       /novnc/core/rfb.js and connects to ws://<this host>:<port>/websockify
//       ?token=<token>. The screen scales to fit TARGET; keyboard and pointer
//       go to the VM after a click. onStatus(kind, text) reports
//       "Connecting…", "Connected", "Disconnected" or why it failed.
//       Returns the noVNC RFB object (disconnect(), sendCtrlAltDel()).
//       consoleModal() wraps it in the console dialog both pages use.
// Why:  spec Tab B ("The packaged noVNC is embedded in the UI");
//       docs/PLAN.md NOTE-16 and NOTE-67.
// =============================================================================

export async function openConsole(api, name, target, onStatus) {
  onStatus("info", "Connecting…");
  const info = await api.post(`/api/vms/${encodeURIComponent(name)}/console`);
  let RFB;
  try {
    ({ default: RFB } = await import("/novnc/core/rfb.js"));
  } catch {
    throw new Error("The console viewer (package novnc) is missing. Reinstall nimbus-control.");
  }
  const rfb = new RFB(target, `ws://${location.hostname}:${info.port}${info.path}`,
    { credentials: { password: info.password } });
  rfb.scaleViewport = true;
  rfb.background = "#05080d";
  rfb.addEventListener("connect", () => { onStatus("good", "Connected. Click the screen to type."); rfb.focus(); });
  rfb.addEventListener("disconnect", (event) => onStatus(event.detail.clean ? "" : "bad",
    event.detail.clean ? "Disconnected." : "The connection was lost. The VM may have turned off."));
  rfb.addEventListener("credentialsrequired", () => rfb.sendCredentials({ password: info.password }));
  rfb.addEventListener("securityfailure", (event) => onStatus("bad", `The VM refused the console: ${event.detail.reason || "wrong password"}.`));
  return rfb;
}

// consoleModal(api, ui, vm): the console in a large dialog, with
// Ctrl+Alt+Del, full screen and "open in its own window" (console.html).
// VM: {name, uuid}. Used by the Virtual Machines page and the Dashboard.
export function consoleModal(api, ui, vm) {
  const screen = ui.h("div", { class: "vnc-screen", tabindex: "-1" });
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
  return m;
}
