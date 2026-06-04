// Sunday onboarding — first-launch wizard.
//
// Pure DOM, no framework. Steps are sections with [data-step]; toggle via
// hidden. Persists the chosen daemon URL + completion flag through the
// preload bridge so main.js skips onboarding on subsequent launches.

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const STEP_ORDER = ['welcome', 'node', 'mic', 'browser', 'done'];

let chosenDaemonHttp = '';
let chosenDaemonWs   = '';
let chosenLabel      = '';

const stepPill = $('#onb-step-pill');
const verifyEl = $('#onb-verify');

function showStep(name) {
  $$('.onb-step').forEach((el) => {
    el.hidden = el.dataset.step !== name;
  });
  const idx = STEP_ORDER.indexOf(name);
  stepPill.textContent = `step ${idx + 1} of ${STEP_ORDER.length}`;
}

// ─── step 1 → 2 ────────────────────────────────────────────────────────

$$('[data-go]').forEach((btn) => {
  btn.addEventListener('click', () => showStep(btn.dataset.go));
});
$$('[data-back]').forEach((btn) => {
  btn.addEventListener('click', () => showStep(btn.dataset.back));
});

// ─── step 2: pick node + test ──────────────────────────────────────────

let chosenToken = '';

// This profile's own daemon address (per-macOS-user port) — fetched from the
// main process at boot; falls back to the classic port if the IPC is slow.
let localURLs = { http: 'http://127.0.0.1:8765', ws: 'ws://127.0.0.1:8765/v1/ws' };
window.sunday.getConfig().then((c) => {
  if (c && c.localHttp) localURLs = { http: c.localHttp, ws: c.localWs };
}).catch(() => {});

function resolveDaemonURLs() {
  const choice = document.querySelector('input[name="node"]:checked').value;
  if (choice === 'local') {
    chosenLabel = 'on this Mac';
    chosenDaemonHttp = localURLs.http;
    chosenDaemonWs   = localURLs.ws;
    chosenToken = '';   // filled from the local daemon's file at finish
  } else {
    const custom = $('#onb-custom-url').value.trim().replace(/\/+$/, '');
    if (!custom) { return null; }
    chosenLabel = `self-hosted · ${custom}`;
    chosenDaemonHttp = custom;
    chosenDaemonWs   = custom.replace(/^http/, 'ws') + '/v1/ws';
    chosenToken = $('#onb-custom-token').value.trim();
  }
  return chosenDaemonHttp;
}

async function testConnection() {
  const choice = document.querySelector('input[name="node"]:checked').value;
  const url = resolveDaemonURLs();
  if (!url) {
    verifyEl.hidden = false; verifyEl.dataset.state = 'fail';
    verifyEl.textContent = 'Enter a URL.';
    return;
  }
  if (choice === 'local') {
    // The embedded daemon is already running locally. Skip straight to the
    // one thing it needs from the user: an OpenRouter key.
    showStep('key');
    return;
  }
  // Self-hosted: verify reachable + token works.
  verifyEl.hidden = false; verifyEl.dataset.state = 'pending';
  verifyEl.textContent = `→ ${url}/v1/health …`;
  try {
    const res = await fetch(`${url}/v1/status`, { headers: chosenToken ? { Authorization: `Bearer ${chosenToken}` } : {} });
    if (res.status === 401) throw new Error('token rejected — check the auth token');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    verifyEl.dataset.state = 'ok';
    verifyEl.textContent = `✓ Sunday ${data.version} · ${data.model}`;
    setTimeout(() => showStep('mic'), 800);
  } catch (err) {
    verifyEl.dataset.state = 'fail';
    verifyEl.textContent = `✗ ${err.message}`;
  }
}
$('#onb-test-conn').addEventListener('click', testConnection);
$('#onb-custom-url').addEventListener('keydown', (e) => { if (e.key === 'Enter') testConnection(); });

// ─── step: give her a brain — fully local (Gemma via Ollama) or keys ───
// The daemon inspects the machine (/v1/local/recommend) and we recommend a
// path; the user always gets both options. Local = Ollama + the Gemma line,
// with a real download progress bar; the memory embedding model rides along.
$('#onb-or-link')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal('https://openrouter.ai/keys'); });

let hwRec = null;   // payload from /v1/local/recommend

async function loadBrainStep() {
  const verdict = $('#onb-hw-verdict');
  try {
    hwRec = await (await fetch(`${chosenDaemonHttp}/v1/local/recommend`, { headers: await daemonAuthHeaders() })).json();
  } catch { verdict.textContent = "couldn't inspect this Mac — pick either path."; return; }
  const { chip, ram_gb, recommendation, models, ollama } = hwRec;
  const localRadio = document.querySelector('input[name="brain"][value="local"]');
  const keysRadio = document.querySelector('input[name="brain"][value="keys"]');
  const sel = $('#onb-local-model');
  if (models && models.length) {
    sel.style.display = '';
    sel.innerHTML = models.map((m) =>
      `<option value="${m.name}" ${m.recommended ? 'selected' : ''}>${m.label} — ${m.note}</option>`).join('');
  }
  if (recommendation === 'keys') {
    verdict.textContent = `This Mac (${chip}, ${ram_gb}GB) is below what local models need to feel good — keys recommended.`;
    if (keysRadio) keysRadio.checked = true;
    $('#onb-local-note').textContent = 'Possible, but this Mac will struggle. Keys are the better experience here.';
  } else {
    const headline = recommendation === 'local' ? 'runs Gemma 4 comfortably' : 'can run a small Gemma well';
    verdict.textContent = `This Mac: ${chip} · ${ram_gb}GB — ${headline}.`;
    $('#onb-local-tag').hidden = false;
    if (localRadio) localRadio.checked = true;
    $('#onb-local-note').textContent = ollama.running
      ? 'Ollama is already running — one download and she thinks entirely on this Mac.'
      : ollama.installed
        ? "Ollama is installed; we'll start it for you."
        : "Needs Ollama (free, one download) — we'll walk you through it.";
  }
}

async function pullWithProgress(model, label) {
  const bar = $('#onb-pullbar'); const fill = $('#onb-pullbar-fill'); const lab = $('#onb-pullbar-label');
  bar.hidden = false; fill.style.width = '0%'; lab.textContent = `downloading ${label}…`;
  const r = await fetch(`${chosenDaemonHttp}/v1/ollama/pull`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
    body: JSON.stringify({ model }),
  });
  if (!r.ok || !r.body) throw new Error(`pull failed (${r.status})`);
  const reader = r.body.getReader(); const dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const lines = buf.split('\n'); buf = lines.pop();
    for (const line of lines) {
      if (!line.trim()) continue;
      let p; try { p = JSON.parse(line); } catch { continue; }
      if (p.error) throw new Error(p.error);
      if (p.total && p.completed != null) {
        const pct = Math.round((100 * p.completed) / p.total);
        fill.style.width = `${pct}%`;
        lab.textContent = `${label}: ${pct}%  (${(p.completed / 1e9).toFixed(1)} / ${(p.total / 1e9).toFixed(1)} GB)`;
      } else if (p.status) {
        lab.textContent = `${label}: ${p.status}`;
      }
    }
  }
  fill.style.width = '100%'; lab.textContent = `${label}: ready`;
}

async function waitForOllama(maxSec) {
  for (let i = 0; i < maxSec; i++) {
    try {
      const d = await (await fetch(`${chosenDaemonHttp}/v1/local/recommend`, { headers: await daemonAuthHeaders() })).json();
      if (d.ollama && d.ollama.running) return true;
    } catch { /* daemon hiccup — keep waiting */ }
    await new Promise((res) => setTimeout(res, 1000));
  }
  return false;
}

$('#onb-save-key')?.addEventListener('click', async () => {
  const v = $('#onb-key-verify');
  const btn = $('#onb-save-key');
  const choice = document.querySelector('input[name="brain"]:checked')?.value || 'keys';
  btn.disabled = true;
  try {
    if (choice === 'keys') {
      const key = $('#onb-or-key').value.trim();
      if (!key) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Paste your key first (or pick fully local).'; return; }
      v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Saving + starting Sunday…';
      const r = await window.sunday.setOpenRouterKey(key);
      if (r.ok) { v.dataset.state = 'ok'; v.textContent = '✓ Sunday is running locally.'; setTimeout(() => showStep('mic'), 800); }
      else { v.dataset.state = 'fail'; v.textContent = `✗ ${r.error || 'failed'}`; }
      return;
    }
    // Fully local: ensure Ollama, pull the chosen Gemma (+ the memory model), flip the brain.
    v.hidden = false; v.dataset.state = 'pending';
    if (!(hwRec && hwRec.ollama && hwRec.ollama.running)) {
      if (hwRec && hwRec.ollama && hwRec.ollama.installed) {
        v.textContent = 'Starting Ollama…';
        await fetch(`${chosenDaemonHttp}/v1/ollama/start`, { method: 'POST', headers: await daemonAuthHeaders() });
      } else {
        v.textContent = "Install Ollama from the page that just opened — I'm watching for it.";
        window.sunday.openExternal('https://ollama.com/download');
      }
      if (!(await waitForOllama(300))) {
        v.dataset.state = 'fail'; v.textContent = "Ollama never came up — install/start it, then hit Continue again."; return;
      }
      hwRec.ollama.running = true;
    }
    const model = $('#onb-local-model').value || 'gemma4:12b';
    v.textContent = 'Downloading the brain — one-time, the big one.';
    await pullWithProgress(model, model);
    if (hwRec && hwRec.embed_model) {
      try { await pullWithProgress(hwRec.embed_model, 'memory model'); } catch { /* memory degrades to FTS; not fatal */ }
    }
    const cfg = await fetch(`${chosenDaemonHttp}/v1/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
      body: JSON.stringify({ provider: 'ollama', model_name: model }),
    });
    const d = await cfg.json().catch(() => ({}));
    if (!cfg.ok) throw new Error(d.error || `config ${cfg.status}`);
    v.dataset.state = 'ok'; v.textContent = `✓ Fully local. ${model} is Sunday's brain — nothing leaves this Mac.`;
    setTimeout(() => showStep('mic'), 1000);
  } catch (err) {
    const m = String(err.message || err);
    v.dataset.state = 'fail';
    if (/newer version of Ollama/i.test(m)) {
      v.textContent = 'Your Ollama is too old for this model — update it from the page that just opened, then hit Continue again.';
      window.sunday.openExternal('https://ollama.com/download');
    } else {
      v.textContent = `✗ ${m}`;
    }
  } finally {
    btn.disabled = false;
  }
});
$('#onb-or-key')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#onb-save-key').click(); });

// ─── step: give Sunday a browser (Playwright extension) ────────────────
// The headline capability — Sunday drives the user's real logged-in browser.
// Connects via the same builtin-connector endpoint Settings uses; the daemon
// is the source of truth for the token hint + extension link.

async function daemonAuthHeaders() {
  let token = chosenToken;
  if (!token && chosenDaemonHttp.includes('127.0.0.1')) {
    try { token = (await window.sunday.localToken()).token || ''; } catch {}
  }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

let pwSetupUrl = 'https://github.com/microsoft/playwright/tree/main/packages/extension#readme';

async function loadBrowserStep() {
  const hint = $('#onb-pw-hint');
  const v = $('#onb-pw-verify');
  try {
    const d = await (await fetch(`${chosenDaemonHttp}/v1/mcp/builtin`, { headers: await daemonAuthHeaders() })).json();
    const pw = (d.connectors || []).find((c) => c.id === 'playwright');
    if (pw) {
      if (pw.token_label && hint) hint.textContent = pw.token_label;
      if (pw.setup_url) pwSetupUrl = pw.setup_url;
      if (pw.enabled && v) { v.hidden = false; v.dataset.state = 'ok'; v.textContent = '✓ Already connected — Sunday can use your browser.'; }
    }
  } catch { /* daemon not reachable yet — the step still works, just skippable */ }
}

$('#onb-pw-install')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal(pwSetupUrl); });
$('#onb-pw-skip')?.addEventListener('click', () => showStep('done'));
$('#onb-pw-connect')?.addEventListener('click', async () => {
  const v = $('#onb-pw-verify');
  const token = $('#onb-pw-token').value.trim();
  if (!token) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Paste the token from the extension first.'; return; }
  v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Connecting to your browser…';
  try {
    const headers = { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) };
    const r = await fetch(`${chosenDaemonHttp}/v1/mcp/builtin`, {
      method: 'POST', headers, body: JSON.stringify({ id: 'playwright', enabled: true, token }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    v.dataset.state = 'ok'; v.textContent = '✓ Sunday can use your browser.';
    setTimeout(() => showStep('done'), 900);
  } catch (err) {
    v.dataset.state = 'fail'; v.textContent = `✗ ${err.message}`;
  }
});
$('#onb-pw-token')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#onb-pw-connect').click(); });

// ─── step 3: microphone permission ─────────────────────────────────────

// ── Microphone permission ──────────────────────────────────────────────
const micStatus = $('#onb-mic-status');
const micBtn    = $('#onb-mic-request');

async function checkMicPermission() {
  if (!navigator.permissions || !navigator.permissions.query) return 'unknown';
  try {
    const s = await navigator.permissions.query({ name: 'microphone' });
    return s.state;
  } catch { return 'unknown'; }
}

function paintMicStatus(state) {
  micStatus.dataset.state = state;
  micStatus.textContent = state;
  micBtn.disabled = state === 'granted';
  micBtn.textContent = state === 'granted' ? 'ready' : 'Grant';
}

async function requestMic() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((t) => t.stop());
    paintMicStatus('granted');
  } catch { paintMicStatus('denied'); }
}
micBtn.addEventListener('click', requestMic);

// ── Full Disk Access ───────────────────────────────────────────────────
const fdaStatus = $('#onb-fda-status');
const fdaBtn    = $('#onb-fda-open');

function paintFdaStatus(state) {
  fdaStatus.dataset.state = state;
  fdaStatus.textContent = state;
  fdaBtn.textContent = state === 'granted' ? 'ready' : 'Open settings';
  fdaBtn.disabled = state === 'granted';
}

async function checkFda() {
  if (!window.sunday?.checkFDA) return 'unknown';
  try {
    const granted = await window.sunday.checkFDA();
    return granted ? 'granted' : 'denied';
  } catch { return 'unknown'; }
}

async function openFdaSettings() {
  if (!window.sunday?.openFDASettings) return;
  await window.sunday.openFDASettings();
  // Re-check shortly after — user typically grants within ~10s.
  setTimeout(async () => paintFdaStatus(await checkFda()), 4000);
}
fdaBtn.addEventListener('click', openFdaSettings);

// Re-check permissions when arriving at the perms step
const _origShow = showStep;
showStep = (name) => {
  _origShow(name);
  if (name === 'mic') {
    checkMicPermission().then(paintMicStatus);
    checkFda().then(paintFdaStatus);
  }
  if (name === 'browser') {
    loadBrowserStep();
  }
  if (name === 'key') {
    loadBrainStep();
  }
  if (name === 'done') {
    $('#onb-summary').textContent = [
      `node:  ${chosenLabel}`,
      `URL:   ${chosenDaemonHttp}`,
    ].join('\n');
  }
};

// ─── step 4: finish ────────────────────────────────────────────────────

$('#onb-finish').addEventListener('click', async () => {
  // For a local install, the token lives in the daemon's own file — grab it
  // so the app authenticates against the embedded daemon.
  let token = chosenToken;
  if (!token && chosenDaemonHttp.includes('127.0.0.1')) {
    try { token = (await window.sunday.localToken()).token || ''; } catch {}
  }
  await window.sunday.finishOnboarding({
    daemonHttp: chosenDaemonHttp,
    daemonWs:   chosenDaemonWs,
    daemonToken: token,
    label:      chosenLabel,
  });
});

// Boot
showStep('welcome');
