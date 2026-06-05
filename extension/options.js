/* Options: optional port override. Empty = auto-discover (the service worker
 * scans the per-user port range and adopts the daemon that accepts our token).
 * Saving triggers a reconnect via the storage.onChanged listener. */

const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get(["port"]);
  $("port").value = s.port || "";
}

async function save() {
  const raw = $("port").value.trim();
  const port = parseInt(raw, 10);
  if (raw === "" || !Number.isFinite(port) || port < 1 || port > 65535) {
    $("port").value = "";
    await chrome.storage.local.remove("port");   // back to auto
  } else {
    $("port").value = port;
    await chrome.storage.local.set({ port });
  }
  const saved = $("saved");
  saved.classList.add("show");
  setTimeout(() => saved.classList.remove("show"), 1500);
}

$("save").addEventListener("click", save);
load();
