/* Options: just the daemon port. Saving triggers a reconnect in the service
 * worker via the storage.onChanged listener. */

const DEFAULT_PORT = 8765;
const $ = (id) => document.getElementById(id);

async function load() {
  const s = await chrome.storage.local.get(["port"]);
  $("port").value = s.port || DEFAULT_PORT;
}

async function save() {
  let port = parseInt($("port").value, 10);
  if (!Number.isFinite(port) || port < 1 || port > 65535) port = DEFAULT_PORT;
  $("port").value = port;
  await chrome.storage.local.set({ port });
  const saved = $("saved");
  saved.classList.add("show");
  setTimeout(() => saved.classList.remove("show"), 1500);
}

$("save").addEventListener("click", save);
load();
