/* Side-panel chat. The service worker owns the WebSocket; this panel just
 * hands it text (background pushes {event:'say'} over the paired socket) and
 * renders the {event:'thinking'} / {event:'reply'} frames the daemon pushes
 * back. No second connection, no extra auth surface. */

const $ = (id) => document.getElementById(id);

const transcript = $("transcript");
const input = $("input");
const sendBtn = $("send");
const form = $("form");
const dot = $("dot");
const stateEl = $("state");
const empty = $("empty");

let thinkingEl = null;

function clearEmpty() {
  if (empty && empty.parentNode) empty.remove();
}

function addMessage(role, text) {
  clearEmpty();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.textContent = text;
  transcript.appendChild(el);
  scrollToBottom();
  return el;
}

function showThinking() {
  clearThinking();
  thinkingEl = document.createElement("div");
  thinkingEl.className = "thinking";
  thinkingEl.textContent = "…";
  transcript.appendChild(thinkingEl);
  scrollToBottom();
}

function clearThinking() {
  if (thinkingEl && thinkingEl.parentNode) thinkingEl.remove();
  thinkingEl = null;
}

function scrollToBottom() {
  transcript.scrollTop = transcript.scrollHeight;
}

function paintState(state) {
  const connected = state === "connected";
  dot.classList.toggle("connected", connected);
  stateEl.textContent = connected
    ? "connected"
    : state === "connecting"
      ? "connecting…"
      : "offline";
  sendBtn.disabled = !connected;
  input.disabled = !connected;
}

function refreshState() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (resp) => {
    if (chrome.runtime.lastError || !resp) return;
    paintState(resp.state);
  });
}

function send() {
  const text = input.value.trim();
  if (!text) return;
  addMessage("user", text);
  input.value = "";
  autoGrow();
  showThinking();
  chrome.runtime.sendMessage({ type: "SAY", text }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.ok) {
      clearThinking();
      addMessage("system", resp?.error ? `couldn't send — ${resp.error}` : "couldn't send.");
    }
  });
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  send();
});

// Enter sends, Shift+Enter newlines.
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

function autoGrow() {
  input.style.height = "auto";
  input.style.height = Math.min(120, input.scrollHeight) + "px";
}
input.addEventListener("input", autoGrow);

// Incoming frames from the background worker.
chrome.runtime.onMessage.addListener((msg) => {
  if (!msg) return;
  if (msg.type === "STATE_CHANGED") {
    paintState(msg.state);
    return;
  }
  if (msg.type === "SAY_EVENT") {
    if (msg.kind === "thinking") {
      showThinking();
    } else if (msg.kind === "reply") {
      clearThinking();
      addMessage("sunday", msg.text || "");
    }
  }
});

refreshState();
setInterval(refreshState, 3000);
