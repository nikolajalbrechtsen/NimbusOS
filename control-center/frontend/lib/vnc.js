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
