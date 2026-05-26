// Sunday settings — load current state from main process + daemon, save back.

const $ = (sel) => document.querySelector(sel);

let DAEMON_HTTP = '';
let DAEMON_WS   = '';
let defaultPrompt = '';
let currentEffectivePrompt = '';

function flashSaved() {
  const el = $('#set-saved');
  el.hidden = false;
  el.textContent = 'saved';
  setTimeout(() => { el.hidden = true; }, 1500);
}

function flashError(msg) {
  const el = $('#set-saved');
  el.hidden = false;
  el.style.color = 'var(--error)';
  el.textContent = msg;
  setTimeout(() => {
    el.hidden = true;
    el.style.color = '';
  }, 3000);
}

async function loadAll() {
  // 1. Connection — from main process prefs
  const cfg = await window.sunday.getConfig();
  DAEMON_HTTP = cfg.daemonHttp;
  DAEMON_WS   = cfg.daemonWs;
  $('#set-http').value = DAEMON_HTTP;
  $('#set-ws').value   = DAEMON_WS;

  // 2. Server config — from daemon
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/config`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const c = await res.json();

    $('#set-provider').value = c.model?.provider || '';
    $('#set-model').value    = c.model?.name     || '';

    defaultPrompt = c.identity_prompt?.default || '';
    currentEffectivePrompt = c.identity_prompt?.effective || '';
    const custom = !!c.identity_prompt?.custom_present;
    $('#set-prompt').value = custom ? currentEffectivePrompt : '';
    $('#set-prompt').placeholder = custom
      ? 'custom personality saved'
      : `using built-in default (${defaultPrompt.length} chars). Type here to override.`;
    $('#set-prompt-status').textContent = custom ? 'custom override active' : 'using default';
    updatePromptCharCount();

    $('#set-mem-avail').textContent = c.memory?.available ? 'yes' : 'no';
    $('#set-mem-count').textContent = String(c.memory?.count ?? '—');
  } catch (err) {
    flashError(`couldn't load config: ${err.message}`);
  }
}

function updatePromptCharCount() {
  const txt = $('#set-prompt').value;
  $('#set-prompt-chars').textContent = `${txt.length} chars`;
}

// ─── Connection ────────────────────────────────────────────────────────

$('#set-conn-test').addEventListener('click', async () => {
  const url = $('#set-http').value.trim().replace(/\/+$/, '');
  const verify = $('#set-conn-verify');
  verify.dataset.state = '';
  verify.textContent = `→ ${url}/v1/status …`;
  try {
    const res = await fetch(`${url}/v1/status`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    verify.dataset.state = 'ok';
    verify.textContent = `✓ ${d.version} · ${d.model} · ${d.messages} msgs · ${(d.devices||[]).length} sat`;
  } catch (err) {
    verify.dataset.state = 'fail';
    verify.textContent = `✗ ${err.message}`;
  }
});

$('#set-conn-save').addEventListener('click', async () => {
  const http = $('#set-http').value.trim().replace(/\/+$/, '');
  const ws   = $('#set-ws').value.trim() || (http.replace(/^http/, 'ws') + '/v1/ws');
  await window.sunday.saveConnection({ daemonHttp: http, daemonWs: ws });
  flashSaved();
});

// ─── Model ─────────────────────────────────────────────────────────────

$('#set-model-save').addEventListener('click', async () => {
  const model = $('#set-model').value.trim();
  if (!model) { flashError('model name required'); return; }
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model_name: model }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    flashSaved();
  } catch (err) {
    flashError(`save failed: ${err.message}`);
  }
});

// ─── Personality ───────────────────────────────────────────────────────

$('#set-prompt').addEventListener('input', updatePromptCharCount);

$('#set-prompt-show-default').addEventListener('click', () => {
  $('#set-prompt').value = defaultPrompt;
  $('#set-prompt-status').textContent = 'showing default — edit and Save to override';
  updatePromptCharCount();
});

$('#set-prompt-reset').addEventListener('click', async () => {
  if (!confirm("Reset Sunday's personality to the default? Your custom prompt will be erased.")) return;
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identity_prompt: null }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadAll();
    flashSaved();
  } catch (err) {
    flashError(`reset failed: ${err.message}`);
  }
});

$('#set-prompt-save').addEventListener('click', async () => {
  const prompt = $('#set-prompt').value;
  try {
    const res = await fetch(`${DAEMON_HTTP}/v1/config`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ identity_prompt: prompt }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await loadAll();
    flashSaved();
  } catch (err) {
    flashError(`save failed: ${err.message}`);
  }
});

$('#set-screen-grant').addEventListener('click', async () => {
  const status = $('#set-screen-status');
  status.textContent = 'requesting…';
  try {
    const r = await window.sunday.requestScreen();
    if (r.status === 'granted') {
      status.textContent = '✓ already granted';
    } else if (r.status === 'prompted') {
      status.textContent = 'enable “Sunday” in the window that opened, then restart Sunday';
    } else {
      status.textContent = `error: ${r.error || 'unknown'}`;
    }
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  }
});

loadAll();
