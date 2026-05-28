// Sunday onboarding — first-launch wizard.
//
// Pure DOM, no framework. Steps are sections with [data-step]; toggle via
// hidden. Persists the chosen daemon URL + completion flag through the
// preload bridge so main.js skips onboarding on subsequent launches.

const $  = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const STEP_ORDER = ['welcome', 'node', 'mic', 'done'];

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

function resolveDaemonURLs() {
  const choice = document.querySelector('input[name="node"]:checked').value;
  if (choice === 'local') {
    chosenLabel = 'local · this Mac';
    chosenDaemonHttp = 'http://127.0.0.1:8765';
    chosenDaemonWs   = 'ws://127.0.0.1:8765/v1/ws';
  } else {
    const custom = $('#onb-custom-url').value.trim().replace(/\/+$/, '');
    if (!custom) { return null; }
    chosenLabel = `self-hosted · ${custom}`;
    chosenDaemonHttp = custom;
    chosenDaemonWs   = custom.replace(/^http/, 'ws') + '/v1/ws';
  }
  return chosenDaemonHttp;
}

async function testConnection() {
  const url = resolveDaemonURLs();
  if (!url) {
    verifyEl.hidden = false;
    verifyEl.dataset.state = 'fail';
    verifyEl.textContent = 'Enter a URL.';
    return;
  }

  verifyEl.hidden = false;
  verifyEl.dataset.state = 'pending';
  verifyEl.textContent = `→ ${url}/v1/status …`;

  try {
    const res = await fetch(`${url}/v1/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    verifyEl.dataset.state = 'ok';
    const dev = (data.devices || []).length;
    verifyEl.textContent = [
      `✓ Sunday ${data.version} · ${data.model}`,
      `  ${data.messages} messages · ${data.tools?.length || 0} tools · ${dev} satellite${dev === 1 ? '' : 's'}`,
    ].join('\n');
    setTimeout(() => showStep('mic'), 800);
  } catch (err) {
    verifyEl.dataset.state = 'fail';
    verifyEl.textContent = `✗ Couldn't reach ${url}: ${err.message}`;
  }
}
$('#onb-test-conn').addEventListener('click', testConnection);
$('#onb-custom-url').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') testConnection();
});

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
  if (name === 'done') {
    $('#onb-summary').textContent = [
      `node:  ${chosenLabel}`,
      `URL:   ${chosenDaemonHttp}`,
    ].join('\n');
  }
};

// ─── step 4: finish ────────────────────────────────────────────────────

$('#onb-finish').addEventListener('click', async () => {
  // Persist + tell main process to open the chat window
  await window.sunday.finishOnboarding({
    daemonHttp: chosenDaemonHttp,
    daemonWs:   chosenDaemonWs,
    label:      chosenLabel,
  });
});

// Boot
showStep('welcome');
