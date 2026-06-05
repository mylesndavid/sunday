/* Background service worker — the WebSocket bridge to Sunday's daemon.
 *
 * Pairing (Playwright-shaped): the extension generates a token on install and
 * shows it in the popup; the user pastes it into Sunday. The extension then
 * connects OUT to ws://127.0.0.1:<port>/v1/cockpit/ws?token=<token> and
 * reconnects with backoff forever. The port is AUTO-DISCOVERED: Sunday's
 * daemon port is per-macOS-user (8765 + uid offset), so we probe the range
 * for live daemons and adopt the one that accepts our token. The options
 * page can pin a port explicitly; leaving it empty means auto.
 *
 * Protocol (frozen contract with the daemon):
 *   daemon → extension   {id, method, params}
 *   extension → daemon   {id, result} | {id, error}
 *   extension events     {event: "status", ...} | {event: "user_done", ...}
 * instruct_user holds the request open and resolves {acknowledged: true} when
 * the user confirms on the page.
 *
 * MV3 service workers idle out after ~30s; WebSocket traffic resets that timer
 * (Chrome 116+), so a 20s status heartbeat keeps the worker alive while
 * connected, and a 30s alarm re-wakes it to reconnect when it isn't.
 */

import { Executor } from "./lib/executor.js";

const DEFAULT_PORT = 8765;
// How many per-user ports to scan upward from the base (uid 501 → 8765,
// 502 → 8766, …). Ten logged-in macOS accounts ought to be enough for anybody.
const PORT_SCAN_SPAN = 10;
const HEARTBEAT_MS = 20000;
const BACKOFF_MIN_MS = 1000;
const BACKOFF_MAX_MS = 30000;

const executor = new Executor();

let ws = null;
let wsState = "disconnected"; // disconnected | connecting | connected
let backoffMs = BACKOFF_MIN_MS;
let reconnectTimer = null;
let heartbeatTimer = null;

/** requestId -> resolve fn for instruct_user requests awaiting the user */
const pendingInstructs = new Map();

// --- Token + settings -------------------------------------------------------

function generateToken() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function getSettings() {
  const s = await chrome.storage.local.get(["token", "port", "lastGoodPort"]);
  let token = s.token;
  if (!token) {
    token = generateToken();
    await chrome.storage.local.set({ token });
  }
  // `port` is a manual override from the options page; empty means
  // auto-discover. Sunday's daemon port is per-macOS-user (8765 + uid offset),
  // so a hardcoded default is wrong for any secondary account.
  const override = Number(s.port) || 0;
  return { token, override, lastGoodPort: Number(s.lastGoodPort) || 0 };
}

// --- Connection lifecycle ----------------------------------------------------

// The port Sunday actually answered on (for the popup's status detail).
let activePort = 0;

/** Ports worth probing: the override pins one; otherwise last-known-good
 * first, then the per-user range (uid 501 → 8765, 502 → 8766, …). */
function candidatePorts({ override, lastGoodPort }) {
  if (override) return [override];
  const out = [];
  if (lastGoodPort) out.push(lastGoodPort);
  for (let p = DEFAULT_PORT; p < DEFAULT_PORT + PORT_SCAN_SPAN; p++) {
    if (!out.includes(p)) out.push(p);
  }
  return out;
}

/** Is a Sunday daemon listening here? /v1/health is auth-exempt and instant. */
async function portAlive(port) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/v1/health`, {
      signal: AbortSignal.timeout(800),
    });
    return r.ok;
  } catch (_) {
    return false;
  }
}

/** Attempt the authed WS handshake. Resolves the open socket, or null — a
 * daemon that's alive but holds a different COCKPIT_TOKEN (another macOS
 * user's Sunday) rejects the upgrade and lands here as null too. */
function tryHandshake(port, token, timeoutMs = 3000) {
  return new Promise((resolve) => {
    let socket;
    try {
      socket = new WebSocket(`ws://127.0.0.1:${port}/v1/cockpit/ws?token=${token}`);
    } catch (_) {
      return resolve(null);
    }
    const timer = setTimeout(() => {
      try { socket.close(); } catch (_) {}
      resolve(null);
    }, timeoutMs);
    socket.onopen = () => { clearTimeout(timer); resolve(socket); };
    socket.onclose = () => { clearTimeout(timer); resolve(null); };
  });
}

async function connect() {
  if (ws && (wsState === "connected" || wsState === "connecting")) return;
  clearTimeout(reconnectTimer);
  const settings = await getSettings();
  wsState = "connecting";
  broadcastState();

  let socket = null;
  for (const port of candidatePorts(settings)) {
    if (!(await portAlive(port))) continue;
    socket = await tryHandshake(port, settings.token);
    if (socket) {
      activePort = port;
      chrome.storage.local.set({ lastGoodPort: port });
      break;
    }
  }
  if (!socket) {
    wsState = "disconnected";
    broadcastState();
    scheduleReconnect();
    return;
  }
  ws = socket;
  wsState = "connected";
  backoffMs = BACKOFF_MIN_MS;

  socket.onmessage = (e) => {
    if (ws !== socket) return;
    handleFrame(e.data);
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    dropConnection();
    scheduleReconnect();
  };
  socket.onerror = () => {
    if (ws !== socket) return;
    try { socket.close(); } catch (_) {}
  };

  sendEvent({
    event: "status",
    state: "connected",
    version: chrome.runtime.getManifest().version,
  });
  startHeartbeat();
  broadcastState();
}

function dropConnection() {
  stopHeartbeat();
  ws = null;
  wsState = "disconnected";
  // Pending instructs can't reach the daemon anymore; drop them.
  pendingInstructs.clear();
  broadcastState();
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, backoffMs);
  backoffMs = Math.min(BACKOFF_MAX_MS, backoffMs * 2);
}

function reconnectNow() {
  clearTimeout(reconnectTimer);
  if (ws) {
    const old = ws;
    ws = null; // make handlers no-ops before closing
    try { old.close(); } catch (_) {}
  }
  stopHeartbeat();
  wsState = "disconnected";
  backoffMs = BACKOFF_MIN_MS;
  connect();
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    sendEvent({ event: "status", state: "connected" });
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(obj)); } catch (_) {}
  }
}
const sendEvent = send;

// --- Request dispatch --------------------------------------------------------

async function handleFrame(data) {
  let msg;
  try {
    msg = JSON.parse(data);
  } catch (_) {
    return; // not ours
  }
  if (!msg) return;

  // Side-panel chat: the daemon pushes {event:'thinking'} then {event:'reply'}
  // in response to a {event:'say'} we sent. Forward them to the side panel.
  if (typeof msg.event === "string") {
    if (msg.event === "thinking") broadcastSay({ kind: "thinking" });
    else if (msg.event === "reply") broadcastSay({ kind: "reply", text: msg.text || "" });
    return;
  }

  if (msg.id == null || typeof msg.method !== "string") return;
  try {
    const result = await dispatch(msg.method, msg.params || {}, msg.id);
    send({ id: msg.id, result: result == null ? {} : result });
  } catch (e) {
    send({ id: msg.id, error: String((e && e.message) || e) });
  }
}

async function dispatch(method, params, requestId) {
  switch (method) {
    case "read_page":   return executor.read_page();
    case "screenshot":  return executor.screenshot();
    case "navigate":    return executor.navigate(params);
    case "click":       return executor.click(params);
    case "fill":        return executor.fill(params);
    case "press_key":   return executor.press_key(params);
    case "scroll":      return executor.scroll(params);
    case "highlight":   return executor.highlight(params);
    case "list_tabs":   return executor.list_tabs();
    case "open_tab":    return executor.open_tab(params);
    case "switch_tab":  return executor.switch_tab(params);
    case "close_tab":   return executor.close_tab(params);
    case "instruct_user": {
      // Show the callout, then hold the request open until the user confirms.
      await executor.instruct_user(params, requestId);
      return new Promise((resolve) => {
        pendingInstructs.set(requestId, resolve);
      });
    }
    default:
      throw new Error(`Unknown method: ${method}`);
  }
}

// --- Messages from content scripts + popup -----------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;

  if (msg.type === "INSTRUCT_DONE") {
    const resolve = pendingInstructs.get(msg.requestId);
    if (resolve) {
      pendingInstructs.delete(msg.requestId);
      resolve({ acknowledged: true });
      sendEvent({ event: "user_done", requestId: msg.requestId });
    }
    return;
  }

  if (msg.type === "GET_STATUS") {
    getSettings().then(({ token, override }) => {
      sendResponse({ state: wsState, token, port: activePort || override || 0 });
    });
    return true; // async
  }

  if (msg.type === "REGENERATE_TOKEN") {
    chrome.storage.local.set({ token: generateToken() }).then(() => {
      reconnectNow();
      getSettings().then(({ token, override }) => sendResponse({ state: wsState, token, port: activePort || override || 0 }));
    });
    return true; // async
  }

  if (msg.type === "RECONNECT") {
    reconnectNow();
    sendResponse({ ok: true });
    return;
  }

  // Side-panel chat → daemon. The side panel can't hold the socket, so it
  // hands us the text and we push {event:'say'} over the paired connection.
  if (msg.type === "SAY") {
    const text = (msg.text || "").trim();
    if (!text) { sendResponse({ ok: false, error: "empty" }); return; }
    if (wsState !== "connected") {
      sendResponse({ ok: false, error: "not connected to Sunday" });
      return;
    }
    send({ event: "say", text });
    sendResponse({ ok: true });
    return;
  }
});

// Tell the popup (if open) that the connection state changed.
function broadcastState() {
  chrome.runtime.sendMessage({ type: "STATE_CHANGED", state: wsState }).catch(() => {});
}

// Tell the side panel (if open) about an incoming chat frame from the daemon.
function broadcastSay(payload) {
  chrome.runtime.sendMessage({ type: "SAY_EVENT", ...payload }).catch(() => {});
}

// Port changes from the options page take effect immediately.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.port) reconnectNow();
});

// --- Keep-alive + startup ----------------------------------------------------

chrome.alarms.create("cockpit-keepalive", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "cockpit-keepalive" && wsState === "disconnected") connect();
});

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
connect(); // also on every service-worker wake
