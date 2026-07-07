// Sunday onboarding — first-launch wizard.
//
// Pure DOM, no framework. Steps are sections with [data-step]; toggle via
// hidden. Persists the chosen daemon URL + completion flag through the
// preload bridge so main.js skips onboarding on subsequent launches.

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

// The 'key' step is branched into from 'node' for local installs, so it isn't
// a linear member of the order. We still want the pill to count it sensibly:
// it sits between 'node' and 'mic', occupying the same slot a self-hosted user
// reaches at 'mic'. STEP_SLOT maps each step to its 1-based position out of
// STEP_TOTAL; 'key' shares its slot with 'mic' so "step 3 of 5" reads right on
// both branches.
const STEP_ORDER = ['welcome', 'node', 'mic', 'browser', 'done'];
const STEP_SLOT = { welcome: 1, node: 2, key: 3, mic: 3, browser: 4, done: 5 };
const STEP_TOTAL = 5;

let chosenDaemonHttp = '';
let chosenDaemonWs   = '';
let chosenLabel      = '';

const stepPill = $('#onb-step-pill');
const verifyEl = $('#onb-verify');

function showStep(name) {
  $$('.onb-step').forEach((el) => {
    el.hidden = el.dataset.step !== name;
  });
  const slot = STEP_SLOT[name] || (STEP_ORDER.indexOf(name) + 1);
  stepPill.textContent = `step ${slot} of ${STEP_TOTAL}`;
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
  const res = await fetch(`${chosenDaemonHttp}/v1/config`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...(await daemonAuthHeaders()) },
    body: JSON.stringify({ provider, credentials: { [credName]: key } }),
  });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || `config ${res.status}`);
}

$('#onb-save-key')?.addEventListener('click', async () => {
  const v = $('#onb-key-verify');
  const btn = $('#onb-save-key');
  const choice = document.querySelector('input[name="brain"]:checked')?.value || 'codex';
  btn.disabled = true;
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
$('#onb-pw-skip')?.addEventListener('click', () => showStep('done'));
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
