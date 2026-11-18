// =============================================================================
// lib/api.js: talking to the Control Center backend
// =============================================================================
// What: get/post/put/del send JSON (putBlob a binary upload chunk) with
//       "Authorization: Bearer <token>" and
//       turn any failure into ApiError(status, message, field), where message
//       is the backend's plain-language explanation. socket(path) opens a
//       WebSocket that authenticates with the subprotocols "nimbus.v1" and
//       "nimbus.token.<token>". exchangeLaunchCode() trades the launcher's
//       one-time code for the token. A 401 calls the onSignedOut handler.
// Why:  docs/PLAN.md §4.3 and NOTE-14: every call carries the per-boot token;
//       the token never appears in a URL.
// =============================================================================

let token = null;
let signedOutHandler = () => {};

export class ApiError extends Error {
  constructor(status, message, field = null) {
    super(message);
    this.status = status;
    this.field = field;
  }
}

export function setToken(value) { token = value; }
export function onSignedOut(handler) { signedOutHandler = handler; }

async function request(method, path, body) {
  const headers = { Authorization: `Bearer ${token}` };
  const init = { method, headers, cache: "no-store" };
  if (body instanceof Blob) {
    headers["Content-Type"] = "application/octet-stream";
    init.body = body;
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new ApiError(0, "The Control Center service isn't answering. It may be restarting; try again in a moment.");
  }
  if (response.status === 401) {
    signedOutHandler();
    throw new ApiError(401, "Your session has ended. Open the Control Center again from the app menu.");
  }
  if (!response.ok) {
    let data = {};
    try { data = await response.json(); } catch { /* not JSON */ }
    throw new ApiError(response.status, data.message || `The request failed (HTTP ${response.status}).`, data.field || null);
  }
  if (response.status === 204) return null;
  return response.json();
}

export const get = (path) => request("GET", path);
export const post = (path, body = {}) => request("POST", path, body);
export const put = (path, body = {}) => request("PUT", path, body);
export const del = (path, body) => request("DELETE", path, body);
export const putBlob = (path, blob) => request("PUT", path, blob);

export async function exchangeLaunchCode(code) {
  let response;
  try {
    response = await fetch("/api/session/exchange", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }), cache: "no-store",
    });
  } catch {
    throw new ApiError(0, "The Control Center service isn't answering.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new ApiError(response.status, data.message || "Signing in failed.");
  return data.token;
}

// socket(path, {onMessage, onClose}): JSON messages in, close reason out.
export function socket(path, { onMessage, onClose, onOpen } = {}) {
  const ws = new WebSocket(`ws://${location.host}${path}`, ["nimbus.v1", `nimbus.token.${token}`]);
  ws.addEventListener("open", () => onOpen && onOpen());
  ws.addEventListener("message", (event) => {
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    onMessage && onMessage(data);
  });
  ws.addEventListener("close", (event) => onClose && onClose(event));
  return ws;
}
