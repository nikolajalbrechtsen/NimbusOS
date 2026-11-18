// =============================================================================
// console.js: the pop-out VM console window (console.html#<vm name>)
// =============================================================================
// What: takes the session token this window inherited from the Control
//       Center window that opened it (sessionStorage is copied into windows a
//       page opens), then shows the VM's screen with a toolbar: status,
//       Ctrl+Alt+Del, full screen, reconnect. Without a session it says how
//       to open a console.
// Why:  spec Tab B (embedded noVNC console); docs/PLAN.md NOTE-67.
// =============================================================================

import * as api from "./lib/api.js";
import * as ui from "./lib/ui.js";
import { openConsole } from "./lib/vnc.js";

const { h, clear } = ui;
const name = decodeURIComponent(location.hash.replace(/^#/, ""));
const token = sessionStorage.getItem("nimbus.token");

function message(text) {
  clear(document.body, h("div", { class: "signed-out" },
    h("div", { class: "panel" }, h("img", { src: "/logo.svg", alt: "" }), h("h1", { text: "VM console" }),
      h("p", { class: "muted", text })))));
}

async function main() {
  if (!token || !name) {
    message("Open a VM's console from the Virtual Machines page of the Nimbus Control Center.");
    return;
  }
  api.setToken(token);
  api.onSignedOut(() => message("Your session has ended. Open the Control Center again from the app menu."));
  document.title = `${name} · VM console`;
  const status = ui.pill("info", "Connecting…");
  const screen = h("div", { class: "vnc-screen", tabindex: "-1" });
  let rfb = null;
  const connect = async () => {
    if (rfb) rfb.disconnect();
    try {
      rfb = await openConsole(api, name, screen, (kind, text) => {
        status.className = `pill ${kind || ""}`.trim();
        status.textContent = text;
      });
    } catch (error) {
      status.className = "pill bad";
      status.textContent = ui.errorText(error);
    }
  };
  clear(document.body, h("div", { class: "console-window" },
    h("header", { class: "console-bar" },
      h("img", { class: "logo", src: "/logo.svg", alt: "" }), h("strong", { class: "mono", text: name }), status,
      h("div", { class: "actions" },
        ui.button({ label: "Ctrl+Alt+Del", small: true, onClick: () => rfb && rfb.sendCtrlAltDel() }),
        ui.button({ small: true, iconName: "restart", kind: "ghost", title: "Reconnect", onClick: connect }),
        ui.button({ small: true, iconName: "fullscreen", kind: "ghost", title: "Full screen",
          onClick: () => screen.requestFullscreen && screen.requestFullscreen() }))),
    screen));
  await connect();
}

main();
