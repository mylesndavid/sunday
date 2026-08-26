// Sunday onboarding — first-launch wizard.
//
// Pure DOM, no framework. Steps are sections with [data-step]; toggle via
// hidden. Persists the chosen daemon URL + completion flag through the
// preload bridge so main.js skips onboarding on subsequent launches.

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// Renderer error bridge — onboarding is exactly where a fresh install breaks, so
// funnel its crashes into the shareable app.log too.
window.addEventListener('error', (e) => {
  window.sunday?.logError?.(`ONBOARDING_ERROR: ${(e.error && e.error.stack) || e.message || (e.filename + ':' + e.lineno)}`);
});
window.addEventListener('unhandledrejection', (e) => {
  window.sunday?.logError?.(`ONBOARDING_REJECTION: ${(e.reason && e.reason.stack) || e.reason}`);
});

// The 'key' step is branched into from 'node' for local installs, so it isn't
// a linear member of the order. We still want the pill to count it sensibly:
// it sits between 'node' and 'mic', occupying the same slot a self-hosted user
// reaches at 'mic'. STEP_SLOT maps each step to its 1-based position out of
// STEP_TOTAL; 'key' shares its slot with 'mic' so "step 3 of 5" reads right on
// both branches.
const STEP_ORDER = ['welcome', 'node', 'mic', 'browser', 'done'];
const STEP_SLOT = { welcome: 1, node: 2, connect: 3, key: 3, mic: 3, browser: 4, email: 5, phone: 6, done: 7 };
// The satellite path is short — connect, permissions, done. Its pill counts
// against its own total so "step 3 of 4" reads honestly on both branches.
const SAT_SLOT = { welcome: 1, node: 2, connect: 3, mic: 4, done: 5 };
const STEP_TOTAL = 7;
const SAT_TOTAL = 5;

let chosenDaemonHttp = '';
let chosenDaemonWs   = '';
let chosenLabel      = '';
let chosenRole       = 'server';   // 'server' | 'satellite'

const stepPill = $('#onb-step-pill');
const verifyEl = $('#onb-verify');

function showStep(name) {
  $$('.onb-step').forEach((el) => {
    el.hidden = el.dataset.step !== name;
  });
  const sat = chosenRole === 'satellite';
  const slot = (sat ? SAT_SLOT[name] : STEP_SLOT[name]) || (STEP_ORDER.indexOf(name) + 1);
  stepPill.textContent = `step ${slot} of ${sat ? SAT_TOTAL : STEP_TOTAL}`;
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
// Prefer daemonHttp (which follows ~/.sunday/daemon.port when the default port was
// busy) over the raw localHttp default — otherwise onboarding POSTs to a port some
// OTHER app is squatting and every step 404s (the "can't get past OpenRouter" bug).
async function refreshDaemonUrl() {
  try {
    const c = await window.sunday.getConfig();
    const http = c && (c.daemonHttp || c.localHttp);
    if (http) {
      const ws = (c.daemonWs || c.localWs || http.replace(/^http/, 'ws') + '/v1/ws');
      localURLs = { http, ws };
      // Keep the local (non-custom) selection pointed at the live daemon URL.
      const node = document.querySelector('input[name="node"]:checked')?.value;
      if (node !== 'satellite') { chosenDaemonHttp = localURLs.http; chosenDaemonWs = localURLs.ws; }
    }
  } catch { /* keep the last-known URL */ }
}
refreshDaemonUrl();

// Escape hatch if setup gets stuck: save a shareable debug packet from onboarding
// (the crash screen isn't reachable here).
$('#onb-debug')?.addEventListener('click', async (e) => {
  e.preventDefault();
  const link = $('#onb-debug');
  const r = await window.sunday?.debugPacket?.();
  if (link) link.textContent = (r && r.ok) ? `Saved to Downloads: ${r.name}` : 'Couldn’t save debug packet';
});

// ── Tailscale awareness ────────────────────────────────────────────────
// The wire between server and satellites. Both roles show its live state up
// front: installed? running? and (once up) this Mac's tailnet address.
async function paintTailscale(el) {
  if (!el || !window.sunday?.tailscaleStatus) return null;
  let ts = null;
  try { ts = await window.sunday.tailscaleStatus(); } catch { return null; }
  el.hidden = false;
  if (!ts.installed) {
    el.dataset.state = 'fail';
    el.textContent = '✗ Tailscale isn’t installed — satellites connect through it. Get it at tailscale.com/download.';
  } else if (!ts.running) {
    el.dataset.state = 'fail';
    el.textContent = ts.error
      ? `✗ Couldn’t read Tailscale’s state (${ts.error}) — if the menu bar says Connected, just continue; the connect test is what counts.`
      : '✗ Tailscale is installed but not running — open the Tailscale app and sign in.';
  } else {
    el.dataset.state = 'ok';
    el.textContent = `✓ Tailscale is up — this Mac is ${ts.dnsName || 'on your tailnet'}.`;
  }
  return ts;
}

function testConnection() {
  chosenRole = document.querySelector('input[name="node"]:checked').value === 'satellite' ? 'satellite' : 'server';
  if (chosenRole === 'satellite') { showStep('connect'); return; }
  // Server: the embedded daemon is already running on this Mac. Next, its brain.
  chosenLabel = 'server · this Mac';
  chosenDaemonHttp = localURLs.http;
  chosenDaemonWs   = localURLs.ws;
  chosenToken = '';   // filled from the local daemon's file at finish
  showStep('key');
}
$('#onb-test-conn').addEventListener('click', testConnection);

// ── satellite: connect to the server over the tailnet ──────────────────
async function connectToServer() {
  const v = $('#onb-sat-verify');
  const url = ($('#onb-sat-url').value || '').trim().replace(/\/+$/, '');
  const token = ($('#onb-sat-token').value || '').trim();
  if (!url) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Enter your server’s Tailscale address.'; return; }
  if (!token) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Enter the auth token shown on the server.'; return; }
  const full = /^https?:\/\//.test(url) ? url : `https://${url}`;
  v.hidden = false; v.dataset.state = 'pending';
  v.textContent = `→ ${full}/v1/status …`;
  try {
    const res = await fetch(`${full}/v1/status`, { headers: { Authorization: `Bearer ${token}` } });
    if (res.status === 401) throw new Error('token rejected — copy it again from the server');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    chosenRole = 'satellite';
    chosenLabel = `satellite → ${full.replace(/^https?:\/\//, '')}`;
    chosenDaemonHttp = full;
    chosenDaemonWs   = full.replace(/^http/, 'ws') + '/v1/ws';
    chosenToken = token;
    v.dataset.state = 'ok';
    v.textContent = `✓ Found your Sunday — ${data.version} · ${data.model}`;
    setTimeout(() => showStep('mic'), 800);
  } catch (err) {
    // Diagnose, don't shrug: the usual culprit is Tailscale on THIS Mac.
    v.dataset.state = 'fail';
    let ts = null;
    try { ts = await window.sunday.tailscaleStatus(); } catch {}
    if (ts && !ts.installed) {
      v.textContent = '✗ Tailscale isn’t installed on this Mac — install it from tailscale.com/download, sign in to your tailnet, then hit Connect again.';
    } else if (ts && !ts.running && !ts.error) {
      v.textContent = '✗ Tailscale isn’t running on this Mac — open the Tailscale app and sign in, then hit Connect again.';
    } else {
      v.textContent = `✗ ${err.message} — this Mac is on the tailnet, so check the address, and that Sunday is running on the server.`;
    }
  }
}
$('#onb-sat-connect')?.addEventListener('click', connectToServer);
$('#onb-sat-token')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') connectToServer(); });

// ─── step: give her a brain ───────────────────────────────────────────────
// Five ways to power the brain — the default is ChatGPT (Codex), which needs no
// key: one sign-in and Sunday uses your subscription. Also OpenRouter/OpenAI/
// Anthropic keys, and fully-local Ollama. The daemon inspects the machine
// (/v1/local/recommend) to annotate the local option; keys/Codex stay first.
$('#onb-or-link')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal('https://openrouter.ai/keys'); });

// Reveal only the selected provider's input.
function syncBrainChoice() {
  const choice = document.querySelector('input[name="brain"]:checked')?.value;
  document.querySelectorAll('.onb-choice .onb-input[data-prov]').forEach((el) => {
    el.hidden = el.dataset.prov !== choice;
    if (!el.hidden) setTimeout(() => el.focus(), 0);
  });
  const sel = $('#onb-local-model');
  if (sel) sel.style.display = (choice === 'local' && sel.options.length) ? '' : 'none';
}
document.querySelectorAll('input[name="brain"]').forEach((r) => r.addEventListener('change', syncBrainChoice));

let hwRec = null;   // payload from /v1/local/recommend

async function loadBrainStep() {
  syncBrainChoice();
  const verdict = $('#onb-hw-verdict');
  try {
    hwRec = await (await fetch(`${chosenDaemonHttp}/v1/local/recommend`, { headers: await daemonAuthHeaders() })).json();
  } catch { verdict.textContent = "couldn't inspect this Mac — any option below works."; return; }
  const { chip, ram_gb, recommendation, models, ollama } = hwRec;
  const sel = $('#onb-local-model');
  if (models && models.length) {
    sel.innerHTML = models.map((m) =>
      `<option value="${m.name}" ${m.recommended ? 'selected' : ''}>${m.label} — ${m.note}</option>`).join('');
  }
  // ChatGPT (Codex) stays the default; we only annotate what local would be like.
  if (recommendation === 'keys') {
    verdict.textContent = `This Mac (${chip}, ${ram_gb}GB) is light for local models — a subscription or key is the smoother ride.`;
    $('#onb-local-note').textContent = 'Possible, but this Mac will struggle with local models.';
  } else {
    verdict.textContent = `This Mac: ${chip} · ${ram_gb}GB — can also run Gemma locally if you'd rather keep everything on-device.`;
    $('#onb-local-tag').hidden = false;
    $('#onb-local-note').textContent = ollama.running
      ? 'Ollama is running — one download and she thinks entirely on this Mac.'
      : ollama.installed
        ? "Ollama is installed; we'll start it for you."
        : "Needs Ollama (free, one download) — we'll walk you through it.";
  }
  syncBrainChoice();
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

// Sign in with ChatGPT (Codex): daemon-side login flips the provider to codex.
async function connectCodex(v) {
  v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Starting ChatGPT sign-in…';
  const res = await fetch(`${chosenDaemonHttp}/v1/codex/login`, { method: 'POST', headers: await daemonAuthHeaders() });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  if (data.connected) return;
  if (!data.auth_url) throw new Error('no sign-in URL returned');
  await window.sunday.openExternal(data.auth_url);
  v.textContent = 'Opening your browser — sign in to ChatGPT, then come back here.';
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1500));
    let s; try { s = await (await fetch(`${chosenDaemonHttp}/v1/codex/status`, { headers: await daemonAuthHeaders() })).json(); } catch { continue; }
    if (s.connected) return;
    if (s.error) throw new Error(s.error);
  }
  throw new Error('timed out waiting for sign-in');
}

// OpenRouter / OpenAI / Anthropic: set the provider + write the key.
async function saveKeyProvider(provider, key) {
  const credName = { openrouter: 'OPENROUTER_API_KEY', openai: 'OPENAI_API_KEY', anthropic: 'ANTHROPIC_API_KEY' }[provider];
  let res;
  try {
    res = await fetch(`${chosenDaemonHttp}/v1/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
      body: JSON.stringify({ provider, credentials: { [credName]: key } }),
    });
  } catch (e) {
    window.sunday?.logError?.(`onboarding ${provider} save NETWORK error → ${chosenDaemonHttp}: ${e && e.message}`);
    throw e;
  }
  const d = await res.json().catch(() => ({}));
  if (!res.ok) {
    window.sunday?.logError?.(`onboarding ${provider} save FAILED: ${d.error || res.status}`);
    throw new Error(d.error || `config ${res.status}`);
  }
}

$('#onb-save-key')?.addEventListener('click', async () => {
  const v = $('#onb-key-verify');
  const btn = $('#onb-save-key');
  const choice = document.querySelector('input[name="brain"]:checked')?.value || 'codex';
  btn.disabled = true;
  await refreshDaemonUrl();   // make sure we talk to the port the daemon ACTUALLY bound
  try {
    if (choice === 'codex') {
      await connectCodex(v);
      v.dataset.state = 'ok'; v.textContent = '✓ Signed in — Sunday thinks with ChatGPT.';
      setTimeout(() => showStep('mic'), 900);
      return;
    }
    if (choice === 'openrouter' || choice === 'openai' || choice === 'anthropic') {
      const key = ($(`#onb-key-${choice}`)?.value || '').trim();
      if (!key) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Paste your key first (or pick another option).'; return; }
      v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Saving + starting Sunday…';
      await saveKeyProvider(choice, key);
      v.dataset.state = 'ok'; v.textContent = '✓ Sunday is running.';
      setTimeout(() => showStep('mic'), 800);
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
document.querySelectorAll('.onb-choice .onb-input[data-prov]').forEach((el) =>
  el.addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#onb-save-key').click(); }));

// ─── step: give Sunday a browser (Cockpit extension) ───────────────────
// The headline capability — Sunday drives the user's real logged-in browser.
// Flow: reveal the bundled Cockpit extension folder → user loads it unpacked
// and copies the token it shows → we save it as the COCKPIT_TOKEN credential
// (the extension connects OUT to the daemon and authenticates with it) → we
// poll /v1/cockpit/status until the extension's socket is live.

async function daemonAuthHeaders() {
  let token = chosenToken;
  if (!token && chosenDaemonHttp.includes('127.0.0.1')) {
    try { token = (await window.sunday.localToken()).token || ''; } catch {}
  }
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function loadBrowserStep() {
  const v = $('#onb-pw-verify');
  // If the extension is already paired AND connected, say so.
  try {
    const d = await (await fetch(`${chosenDaemonHttp}/v1/cockpit/status`, { headers: await daemonAuthHeaders() })).json();
    if (d.connected && v) {
      v.hidden = false; v.dataset.state = 'ok';
      v.textContent = '✓ Already connected — Sunday can use your browser.';
    }
  } catch { /* daemon not reachable yet — the step still works, just skippable */ }
}

async function pollCockpitConnected(maxSec) {
  const v = $('#onb-pw-verify');
  for (let i = 0; i < maxSec; i++) {
    try {
      const d = await (await fetch(`${chosenDaemonHttp}/v1/cockpit/status`, { headers: await daemonAuthHeaders() })).json();
      if (d.connected) return true;
      // Wrong-token knock seen by the daemon: the pasted token is stale
      // (extension reinstall regenerates it). Tell the user mid-poll instead
      // of timing out into a generic failure.
      if (d.token_mismatch && v) {
        v.hidden = false; v.dataset.state = 'fail';
        v.textContent = 'The extension shows a different token now — copy it again and hit Connect.';
      }
    } catch { /* daemon hiccup — keep polling */ }
    await new Promise((res) => setTimeout(res, 1000));
  }
  return false;
}

$('#onb-pw-install')?.addEventListener('click', async (e) => {
  e.preventDefault();
  try { await window.sunday.revealExtension(); } catch { /* hint text covers it */ }
});
// Deep-link chrome://extensions via the main process (AppleScript — Chrome
// swallows chrome:// URLs from openExternal, so that's not a usable fallback).
$('#onb-pw-chrome')?.addEventListener('click', async (e) => {
  e.preventDefault();
  try { await window.sunday.openChromeExtensions(); } catch { /* hint text covers it */ }
});
$('#onb-pw-skip')?.addEventListener('click', () => showStep('email'));
$('#onb-pw-connect')?.addEventListener('click', async () => {
  const v = $('#onb-pw-verify');
  const token = $('#onb-pw-token').value.trim();
  if (!token) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Paste the token from the extension first.'; return; }
  v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Saving token, waiting for your browser to connect…';
  try {
    // Save the pairing token as a credential. The extension connects OUT to the
    // daemon and its ?token= is checked against this stored COCKPIT_TOKEN.
    const headers = { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) };
    const r = await fetch(`${chosenDaemonHttp}/v1/config`, {
      method: 'POST', headers, body: JSON.stringify({ credentials: { COCKPIT_TOKEN: token } }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
    // Now wait for the extension to actually connect.
    if (await pollCockpitConnected(60)) {
      v.dataset.state = 'ok'; v.textContent = '✓ Sunday can use your browser.';
      setTimeout(() => showStep('done'), 900);
    } else {
      v.dataset.state = 'fail';
      v.textContent = '✗ Token saved, but the extension hasn\'t connected yet. Make sure it\'s loaded in Chrome — you can also finish this later from Settings.';
    }
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
  if (name === 'node') {
    paintTailscale($('#onb-ts-status'));
  }
  if (name === 'connect') {
    paintTailscale($('#onb-sat-ts'));
  }
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
      `role:  ${chosenLabel}`,
      `URL:   ${chosenDaemonHttp}`,
    ].join('\n');
    if (chosenRole === 'server') loadPairInfo();
  }
};

// After the permissions step, a satellite is done — the brain, browser, email,
// and phone all live on the server. Only a server configures those here.
$('#onb-mic-continue')?.addEventListener('click', () => {
  showStep(chosenRole === 'satellite' ? 'done' : 'browser');
});

// Server finish screen: put the brain on the tailnet and show exactly what to
// type on the other Macs — address + token. That pair IS satellite onboarding.
async function loadPairInfo() {
  const box = $('#onb-pair'); const info = $('#onb-pair-info');
  if (!box || !info) return;
  try {
    const net = await window.sunday.setupServerNetwork();   // idempotent tailscale serve
    const si  = await window.sunday.serverInfo();
    if (net.ok && net.url && si.token) {
      box.hidden = false;
      info.textContent = `address: ${net.url}\ntoken:   ${si.token}`;
    } else if (si.tailscale && !si.tailscale.running) {
      box.hidden = false;
      info.textContent = 'Tailscale isn’t running on this Mac yet — start it, then find the address + token in Settings.';
    }
  } catch { /* pairing info is a bonus — never block finishing */ }
}

// ─── step: Sunday's own email (AgentMail) — save, verify it reaches an inbox, ──
// then let the user send themselves a real test. Optional; Skip → phone.
$('#onb-am-link')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal('https://agentmail.to'); });
$('#onb-am-skip')?.addEventListener('click', () => showStep('phone'));

let amConnected = false;
$('#onb-am-save')?.addEventListener('click', async () => {
  if (amConnected) { showStep('phone'); return; }        // second click = Continue
  const v = $('#onb-am-verify'); const btn = $('#onb-am-save');
  const key = ($('#onb-am-key')?.value || '').trim();
  if (!key) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Paste your AgentMail key (or Skip).'; return; }
  btn.disabled = true; v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Saving + reaching your inbox…';
  try {
    await fetch(`${chosenDaemonHttp}/v1/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
      body: JSON.stringify({ credentials: { AGENTMAIL_API_KEY: key } }),
    });
    let addr = null;
    for (let i = 0; i < 12; i++) {   // poll until the inbox actually connects
      let d; try { d = await (await fetch(`${chosenDaemonHttp}/v1/channels/agentmail/status`, { headers: await daemonAuthHeaders() })).json(); } catch { d = {}; }
      if (d.connected) { addr = d.address || 'inbox ready'; break; }
      if (d.configured && d.error) throw new Error(d.error);
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (!addr) throw new Error("Key saved, but the inbox didn't connect — double-check the key.");
    amConnected = true;
    v.dataset.state = 'ok'; v.textContent = '✓ Connected.';
    $('#onb-am-key').hidden = true;
    $('#onb-am-addr').textContent = addr;
    $('#onb-am-connected').hidden = false;
    btn.textContent = 'Continue';
  } catch (err) { v.dataset.state = 'fail'; v.textContent = `✗ ${err.message}`; }
  finally { btn.disabled = false; }
});
$('#onb-am-test')?.addEventListener('click', async () => {
  const v = $('#onb-am-test-verify'); const btn = $('#onb-am-test');
  const to = ($('#onb-am-test-to')?.value || '').trim();
  if (!to) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Enter your email to send yourself a test.'; return; }
  btn.disabled = true; v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Sending…';
  try {
    const d = await (await fetch(`${chosenDaemonHttp}/v1/channels/agentmail/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
      body: JSON.stringify({ to }),
    })).json();
    if (d.ok) { v.dataset.state = 'ok'; v.textContent = `✓ Sent to ${to} — check your inbox. Reply and it lands in your chat.`; }
    else { v.dataset.state = 'fail'; v.textContent = `✗ ${d.error || 'send failed'}`; }
  } catch (err) { v.dataset.state = 'fail'; v.textContent = `✗ ${err.message}`; }
  finally { btn.disabled = false; }
});

// ─── step: Sunday's own phone (VAPI) — save, verify configured, then a real ──
// test call to the user's number. Optional; Skip → done.
$('#onb-vapi-link')?.addEventListener('click', (e) => { e.preventDefault(); window.sunday.openExternal('https://vapi.ai'); });
$('#onb-vapi-skip')?.addEventListener('click', () => showStep('done'));

let vapiConnected = false;
$('#onb-vapi-save')?.addEventListener('click', async () => {
  if (vapiConnected) { showStep('done'); return; }
  const v = $('#onb-vapi-verify'); const btn = $('#onb-vapi-save');
  const key = ($('#onb-vapi-key')?.value || '').trim();
  const num = ($('#onb-vapi-number')?.value || '').trim();
  if (!key || !num) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Enter both the API key and phone number id (or Skip).'; return; }
  btn.disabled = true; v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Saving…';
  try {
    await fetch(`${chosenDaemonHttp}/v1/config`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
      body: JSON.stringify({ credentials: { VAPI_API_KEY: key, VAPI_PHONE_NUMBER_ID: num } }),
    });
    let d; try { d = await (await fetch(`${chosenDaemonHttp}/v1/vapi/status`, { headers: await daemonAuthHeaders() })).json(); } catch { d = {}; }
    if (!d.configured) throw new Error('Saved, but VAPI reports not configured — check the key + number id.');
    vapiConnected = true;
    v.dataset.state = 'ok'; v.textContent = '✓ Configured.';
    $('#onb-vapi-key').hidden = true;
    $('#onb-vapi-connected').hidden = false;
    btn.textContent = 'Continue';
  } catch (err) { v.dataset.state = 'fail'; v.textContent = `✗ ${err.message}`; }
  finally { btn.disabled = false; }
});
$('#onb-vapi-test')?.addEventListener('click', async () => {
  const v = $('#onb-vapi-test-verify'); const btn = $('#onb-vapi-test');
  const to = ($('#onb-vapi-test-to')?.value || '').trim();
  if (!to) { v.hidden = false; v.dataset.state = 'fail'; v.textContent = 'Enter your number and Sunday will call it.'; return; }
  btn.disabled = true; v.hidden = false; v.dataset.state = 'pending'; v.textContent = 'Placing the call…';
  try {
    const d = await (await fetch(`${chosenDaemonHttp}/v1/vapi/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
      body: JSON.stringify({ to }),
    })).json();
    if (d.ok) { v.dataset.state = 'ok'; v.textContent = `✓ Calling ${to} now — pick up. The transcript lands in your chat.`; }
    else { v.dataset.state = 'fail'; v.textContent = `✗ ${d.error || 'call failed'}`; }
  } catch (err) { v.dataset.state = 'fail'; v.textContent = `✗ ${err.message}`; }
  finally { btn.disabled = false; }
});

// ─── step: finish ────────────────────────────────────────────────────────

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
    role:       chosenRole,
  });
});

// Boot
showStep('welcome');
