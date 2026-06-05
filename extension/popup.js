/* Popup: pairing token + live connection status. The service worker owns the
 * WebSocket; this page only queries it and triggers token actions. */

const $ = (id) => document.getElementById(id);

function render({ state, token, port }) {
  if (token != null) $("token").value = token;
  const dot = $("dot");
  const label = $("statusLabel");
  const detail = $("statusDetail");
  dot.classList.toggle("connected", state === "connected");
  if (state === "connected") {
    label.textContent = "Connected to Sunday";
    detail.textContent = `127.0.0.1:${port}`;
  } else {
    label.textContent = state === "connecting" ? "Connecting…" : "Waiting for Sunday";
    detail.textContent = `Looking for Sunday on 127.0.0.1:${port}`;
  }
}

function refresh() {
  chrome.runtime.sendMessage({ type: "GET_STATUS" }, (resp) => {
    if (chrome.runtime.lastError || !resp) return;
    render(resp);
  });
}

$("copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("token").value);
  $("copy").textContent = "Copied";
  setTimeout(() => ($("copy").textContent = "Copy"), 1200);
});

$("regenerate").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "REGENERATE_TOKEN" }, (resp) => {
    if (chrome.runtime.lastError || !resp) return;
    render(resp);
  });
});

$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());

// Open the side-panel chat in the current window. Must be called from this
// user gesture; the popup stays the primary surface for the pairing token.
$("chat").addEventListener("click", async () => {
  try {
    const win = await chrome.windows.getCurrent();
    await chrome.sidePanel.open({ windowId: win.id });
    window.close();
  } catch (_) {
    // older Chrome without sidePanel.open — fall back to enabling it
    try { await chrome.sidePanel.setOptions({ enabled: true }); } catch (__) {}
  }
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "STATE_CHANGED") refresh();
});

refresh();
// Re-poll while open: connection state can change without an event reaching us
// (e.g. the worker was asleep when the popup opened).
setInterval(refresh, 2000);
