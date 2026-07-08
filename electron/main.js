// Sunday desktop — Electron main process.
//
// The main process is intentionally thin: it spawns the right first window
// (onboarding on fresh install, main chat after), an always-on-top overlay
// pill, and brokers a couple of small IPC calls.
//
// On launch:
//   1. Read prefs.json from app.getPath('userData')
//   2. If onboarded → open main chat with the saved daemon URL
//   3. Else → open onboarding window; finishOnboarding() saves prefs +
//      opens the main chat.
//
// Env vars SUNDAY_DAEMON_HTTP / SUNDAY_DAEMON_WS, when set, win over saved
// prefs (lets you override without re-onboarding).

const { app, BrowserWindow, Tray, Menu, MenuItem, ipcMain, shell, nativeImage, desktopCapturer, systemPreferences, nativeTheme } = require('electron');

// Window chrome follows the macOS appearance so dark mode has no light flash
// and the hidden-inset titlebar strip matches. Mirrors theme.css's --bg tokens.
function windowBg() { return nativeTheme.shouldUseDarkColors ? '#191612' : '#fbfaf7'; }
// Live-update open windows when the system flips light/dark (the CSS flips
// instantly via prefers-color-scheme; this keeps the native titlebar strip in step).
nativeTheme.on('updated', () => {
  const bg = windowBg();
  for (const w of BrowserWindow.getAllWindows()) { try { w.setBackgroundColor(bg); } catch { /* gone */ } }
});
const { autoUpdater } = require('electron-updater');
const path = require('node:path');
const fs   = require('node:fs');
const os   = require('node:os');
const satellite = require('./satellite');

const PREFS_FILE = () => path.join(app.getPath('userData'), 'prefs.json');

// Per-macOS-user daemon port. The first/primary account (uid 501) keeps the
// historical 8765 so existing installs don't move; each additional profile
// gets its own (502 → 8766, …). Two logged-in profiles can never fight over
// one port — you can't even kill a foreign user's daemon (cross-user signals
// are denied), so sharing the port was never going to work.
const LOCAL_PORT = 8765 + Math.max(0, ((os.userInfo().uid ?? 501) - 501));

function loadPrefs() {
  try { return JSON.parse(fs.readFileSync(PREFS_FILE(), 'utf-8')); }
  catch { return {}; }
}
function savePrefs(patch) {
  const next = { ...loadPrefs(), ...patch };
  fs.mkdirSync(path.dirname(PREFS_FILE()), { recursive: true });
  fs.writeFileSync(PREFS_FILE(), JSON.stringify(next, null, 2));
  return next;
}

function resolveDaemon() {
  const prefs = loadPrefs();
  // Migrate fixed-port prefs saved before per-user ports existed: a secondary
  // profile whose prefs still say :8765 would collide with another profile's
  // daemon forever. Local URLs only — cloud URLs pass through untouched.
  if (LOCAL_PORT !== 8765 && /^http:\/\/(127\.0\.0\.1|localhost):8765$/.test(prefs.daemonHttp || '')) {
    prefs.daemonHttp = `http://127.0.0.1:${LOCAL_PORT}`;
    prefs.daemonWs   = `ws://127.0.0.1:${LOCAL_PORT}/v1/ws`;
    savePrefs({ daemonHttp: prefs.daemonHttp, daemonWs: prefs.daemonWs });
    console.log(`migrated local daemon prefs to per-user port ${LOCAL_PORT}`);
  }
  // Auth token: prefer the saved pref; if missing AND the daemon is local
  // (127.0.0.1), read the daemon's own token file. Lets the same code path
  // work for both "local daemon on this Mac" and "remote daemon I pasted in
  // during onboarding".
  let daemonToken = prefs.daemonToken || process.env.SUNDAY_DAEMON_TOKEN || '';
  const httpUrl = process.env.SUNDAY_DAEMON_HTTP || prefs.daemonHttp || `http://127.0.0.1:${LOCAL_PORT}`;
  if (!daemonToken && /^http:\/\/(127\.0\.0\.1|localhost)/.test(httpUrl)) {
    try {
      const p = path.join(os.homedir(), '.sunday', 'auth.token');
      if (fs.existsSync(p)) daemonToken = fs.readFileSync(p, 'utf8').trim();
    } catch {}
  }
  return {
    daemonHttp: httpUrl,
    daemonWs:   process.env.SUNDAY_DAEMON_WS   || prefs.daemonWs   || `ws://127.0.0.1:${LOCAL_PORT}/v1/ws`,
    daemonToken,
    // Onboarded once we have BOTH a usable token (from prefs, or the local
    // daemon's own file) AND the onboarding flag. The file-read covers a
    // fresh local install where the embedded daemon minted the token but the
    // user hasn't saved anything to prefs yet.
    onboarded:  !!daemonToken && !!prefs.onboarded,
  };
}

// This machine's role in the topology: "server" (this Mac IS the brain — daemon
// always-on, owns the one chat, reachable over Tailscale) or "satellite" (a
// window onto a brain that lives elsewhere). An explicit prefs.role wins;
// otherwise derive it from where the daemon lives — a local brain IS the server,
// a remote one makes this a satellite. Role is the topology concept; the
// local/cloud mechanism it derives from stays underneath it.
function resolveRole() {
  const prefs = loadPrefs();
  if (prefs.role === 'server' || prefs.role === 'satellite') return prefs.role;
  return isLocalDaemon() ? 'server' : 'satellite';
}


// Auth helper for main-process fetches against the daemon. Returns the
// headers to merge into a fetch call so we always carry the bearer.
function _bearer() {
  const { daemonToken } = resolveDaemon();
  return daemonToken ? { 'Authorization': `Bearer ${daemonToken}` } : {};
}

// ── Embedded daemon ─────────────────────────────────────────────────────
// Sunday is two pieces: this desktop UI + a Python daemon (the brain). We
// ship the daemon as a PyInstaller binary inside the app so a fresh install
// needs zero terminal — the app spawns it on launch when the configured
// daemon is local. Remote daemons (a self-hosted VPS) are left alone.
let daemonChild = null;

function bundledDaemonBinary() {
  const rel = app.isPackaged
    ? path.join(process.resourcesPath, 'sunday-daemon', 'sunday-daemon')
    : path.join(__dirname, 'build', 'daemon-dist', 'sunday-daemon', 'sunday-daemon');
  return fs.existsSync(rel) ? rel : null;
}

function isLocalDaemon() {
  const { daemonHttp } = resolveDaemon();
  return /^http:\/\/(127\.0\.0\.1|localhost)/.test(daemonHttp);
}

async function daemonHealthy() {
  try {
    const { daemonHttp } = resolveDaemon();
    const res = await fetch(`${daemonHttp}/v1/health`, { signal: AbortSignal.timeout(1500) });
    return res.ok;
  } catch { return false; }
}

// The local bearer token. The APP owns it: read ~/.sunday/auth.token, or mint
// one and persist it. We hand this to the daemon via env so the two always
// agree — the daemon used to mint its own in memory and could drift from what
// the app reads off disk, breaking This Mac auth.
function localAuthToken() {
  const p = path.join(os.homedir(), '.sunday', 'auth.token');
  try { const t = fs.readFileSync(p, 'utf8').trim(); if (t) return t; } catch {}
  const tok = 'sunday_' + require('node:crypto').randomBytes(32).toString('base64url');
  try { fs.mkdirSync(path.dirname(p), { recursive: true }); fs.writeFileSync(p, tok, { mode: 0o600 }); } catch {}
  return tok;
}

// Does the daemon on 8765 accept OUR token? A stale daemon (old token cached in
// memory) is healthy but unauthable — we must not reuse it.
async function daemonAcceptsToken(token) {
  try {
    const res = await fetch(`${LOCAL_HTTP}/v1/auth/check`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }), signal: AbortSignal.timeout(1500),
    });
    return res.ok;
  } catch { return false; }
}

// Is the running daemon OUR version? After an auto-update the old daemon keeps
// running (token still valid) with stale code — e.g. missing newly-added routes.
// /v1/health reports the app version that spawned it (SUNDAY_APP_VERSION); a
// mismatch means it predates this app and must be restarted.
async function daemonMatchesVersion() {
  try {
    const { daemonHttp } = resolveDaemon();
    const res = await fetch(`${daemonHttp}/v1/health`, { signal: AbortSignal.timeout(1500) });
    if (!res.ok) return false;
    const j = await res.json().catch(() => ({}));
    return j.version === app.getVersion();
  } catch { return false; }
}

// Kill whatever is squatting the local daemon port (a stale daemon from a prior
// run that won't accept our token). Best-effort; macOS/Linux.
function killStaleDaemon() {
  try {
    require('node:child_process').execSync(
      `lsof -ti tcp:${LOCAL_PORT} -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null; pkill -9 -f sunday-daemon/sunday-daemon 2>/dev/null`,
      { stdio: 'ignore', shell: '/bin/bash' });
  } catch {}
}

async function startEmbeddedDaemon() {
  if (daemonChild) return true;
  if (!isLocalDaemon()) return false;          // remote daemon — not ours to run
  // SERVER role (packaged): launchd owns the daemon so it outlives the GUI —
  // survives app quit, crash, and reboot. We install/refresh the agent and wait
  // for health; we never spawn an app-child or pkill (KeepAlive would fight it).
  // SAFETY NET: if launchd can't bring the daemon up, fall through to the
  // app-child path so the brain is never left dead — a child beats no brain.
  if (wantsLaunchdDaemon()) {
    if (await installServerDaemon()) return true;
    console.warn('launchd daemon failed to come up — falling back to an app-child daemon');
  }
  const token = localAuthToken();
  // Reuse an already-running local daemon ONLY if it accepts our token AND runs
  // our version; otherwise it's stale (bad token, or old code left over from a
  // prior version across an auto-update) — kill it and spawn a fresh one.
  if (await daemonHealthy()) {
    if (await daemonAcceptsToken(token) && await daemonMatchesVersion()) return true;
    console.warn('local daemon on 8765 is stale (token or version mismatch) — killing it');
    killStaleDaemon();
    await new Promise((r) => setTimeout(r, 1000));
  }
  const bin = bundledDaemonBinary();
  if (!bin) { console.warn('no bundled daemon binary'); return false; }
  console.log('spawning embedded daemon:', bin);
  daemonChild = require('node:child_process').spawn(bin, [], {
    stdio: 'ignore',
    // app-owned token wins; app version lets us detect a stale daemon next launch;
    // SUNDAY_PORT binds this profile's own port (per-user, no cross-profile fights);
    // SUNDAY_ARGUS_URL (only when the Argus toggle is on) makes the brain ship traces.
    env: { ...process.env, SUNDAY_AUTH_TOKEN: token, SUNDAY_APP_VERSION: app.getVersion(),
           SUNDAY_PORT: String(LOCAL_PORT),
           ...(loadPrefs().argus ? { SUNDAY_ARGUS_URL: ARGUS_URL } : {}) },
    detached: false,
  });
  daemonChild.on('exit', (code) => { console.warn('daemon exited', code); daemonChild = null; });
  daemonChild.on('error', (e) => { console.warn('daemon spawn error', e?.message); daemonChild = null; });
  // Wait up to ~20s for it to come up AND accept our token.
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await daemonHealthy() && await daemonAcceptsToken(token)) { console.log('embedded daemon healthy'); return true; }
  }
  console.warn('embedded daemon never became healthy');
  return false;
}

function stopEmbeddedDaemon() {
  try { daemonChild?.kill('SIGTERM'); } catch {}
  daemonChild = null;
}

// ── Server daemon (launchd, always-on) ──────────────────────────────────
// The doctrine: the mini is always-on and canonical. So a SERVER doesn't run
// the brain as an app child that dies when you quit Sunday — it hands ownership
// to launchd (RunAtLoad + KeepAlive), which starts the daemon at boot and
// restarts it on crash. The app only installs/refreshes the agent and waits for
// health. Satellites and dev builds keep the app-child path above. Mutual
// exclusion is the whole game: launchd OR app-child owns the port, never both.
const SERVER_DAEMON_LABEL = 'com.sunday.daemon';
function serverDaemonPlistPath() {
  return path.join(os.homedir(), 'Library', 'LaunchAgents', `${SERVER_DAEMON_LABEL}.plist`);
}
function serverDaemonInstalled() {
  try { return fs.existsSync(serverDaemonPlistPath()); } catch { return false; }
}
// Should launchd own the daemon? Only a packaged server pointed at the local
// daemon — dev builds (no stable bundled-binary path, run from a venv) keep the
// app-child flow, which also spares developer machines a surprise LaunchAgent.
function wantsLaunchdDaemon() {
  return app.isPackaged && resolveRole() === 'server' && isLocalDaemon() && !!bundledDaemonBinary();
}

function _xmlEscape(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function _serverDaemonPlistXml(bin, token) {
  const log = path.join(os.homedir(), '.sunday', 'logs', 'daemon-launchd.log');
  // Mirror the env the app-child daemon gets (startEmbeddedDaemon). launchd hands
  // an agent a minimal environment, so set PATH explicitly — the brain shells out
  // to tools (codex, tailscale, …) that live in these dirs.
  const env = {
    SUNDAY_AUTH_TOKEN: token,
    SUNDAY_APP_VERSION: app.getVersion(),
    SUNDAY_PORT: String(LOCAL_PORT),
    PATH: '/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
    ...(loadPrefs().argus ? { SUNDAY_ARGUS_URL: ARGUS_URL } : {}),
  };
  const envXml = Object.entries(env)
    .map(([k, v]) => `      <key>${k}</key><string>${_xmlEscape(v)}</string>`).join('\n');
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${SERVER_DAEMON_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${_xmlEscape(bin)}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
${envXml}
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${_xmlEscape(log)}</string>
  <key>StandardErrorPath</key><string>${_xmlEscape(log)}</string>
</dict>
</plist>
`;
}
function _launchctl(args) {
  // Absolute path — a Finder-launched app can have a minimal PATH that omits /bin.
  try { require('node:child_process').execFileSync('/bin/launchctl', args, { stdio: 'ignore', timeout: 8000 }); return true; }
  catch { return false; }
}

// Install (or refresh) and start the launchd daemon. Idempotent: rewrites the
// plist with the current token / version / argus env, reloads it so a post-update
// launch restarts the daemon onto the new binary, and waits for it to answer our
// token. Returns true on health.
async function installServerDaemon() {
  const bin = bundledDaemonBinary();
  if (!bin) return false;
  const token = localAuthToken();
  const uid = os.userInfo().uid ?? 501;
  const domain = `gui/${uid}`, target = `${domain}/${SERVER_DAEMON_LABEL}`;
  const plist = serverDaemonPlistPath();
  // Decide BEFORE touching anything: is a healthy, current daemon already serving?
  // If so we must NOT bootout — bootout-then-bootstrap can race and leave the
  // brain dead (the 0.4.76 relaunch bug). Only reload when we actually need to:
  // the daemon is down, or it's stale (old code left over after an auto-update).
  const alreadyGood = await daemonHealthy() && await daemonAcceptsToken(token) && await daemonMatchesVersion();
  try {
    fs.mkdirSync(path.dirname(plist), { recursive: true });
    fs.mkdirSync(path.join(os.homedir(), '.sunday', 'logs'), { recursive: true });
    // 0600 — the plist embeds the bearer token, so keep it owner-only.
    fs.writeFileSync(plist, _serverDaemonPlistXml(bin, token), { mode: 0o600 });
  } catch (e) { console.warn('write daemon plist failed', e?.message); return false; }
  if (alreadyGood) return true;   // serving and current — leave it running, just refresh the plist on disk
  // Reload. bootout first so bootstrap re-reads the (possibly changed) plist;
  // wait long enough for it to FULLY exit (a short wait was the race that killed
  // the brain), clear any non-launchd squatter, then bootstrap with retries and
  // verify health each round.
  _launchctl(['bootout', target]);
  await new Promise((r) => setTimeout(r, 1200));
  try {
    require('node:child_process').execSync(
      `lsof -ti tcp:${LOCAL_PORT} -sTCP:LISTEN 2>/dev/null | xargs -r kill -9 2>/dev/null`,
      { stdio: 'ignore', shell: '/bin/bash' });
  } catch {}
  for (let attempt = 0; attempt < 3; attempt++) {
    // bootstrap loads + starts (RunAtLoad); if it's somehow already loaded, kickstart restarts it.
    if (!_launchctl(['bootstrap', domain, plist])) _launchctl(['kickstart', '-k', target]);
    for (let i = 0; i < 24; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (await daemonHealthy() && await daemonAcceptsToken(token)) { console.log('launchd daemon healthy'); return true; }
    }
    console.warn(`launchd daemon not healthy (attempt ${attempt + 1}/3) — booting out and retrying`);
    _launchctl(['bootout', target]);
    await new Promise((r) => setTimeout(r, 1500));
  }
  console.warn('launchd daemon never became healthy');
  return false;
}

// Tear down the launchd daemon — used when this Mac stops being the server.
function removeServerDaemon() {
  const uid = os.userInfo().uid ?? 501;
  _launchctl(['bootout', `gui/${uid}/${SERVER_DAEMON_LABEL}`]);
  try { fs.unlinkSync(serverDaemonPlistPath()); } catch {}
}

// Restart the local daemon to reload credentials / env. Launchd-managed servers
// are reloaded in place (which rewrites the plist and picks up disk creds);
// app-child daemons are killed and respawned. Never pkill a launchd daemon.
async function restartLocalDaemon() {
  if (!isLocalDaemon()) return;
  if (wantsLaunchdDaemon()) { await installServerDaemon(); return; }
  killStaleDaemon();
  daemonChild = null;
  await new Promise((r) => setTimeout(r, 800));
  await startEmbeddedDaemon();
}

let mainWindow = null;
let overlayWindow = null;
let onboardingWindow = null;
let tray = null;

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 980,
    height: 720,
    minWidth: 540,
    minHeight: 480,
    backgroundColor: windowBg(),
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 14, y: 14 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));
  mainWindow.on('closed', () => { mainWindow = null; });

  // Open external links in the user's real browser, not inside the app.
  // setWindowOpenHandler only covers window.open / target=_blank. An inline
  // <a href> in the chat thread triggers a top-level navigation instead, which
  // would replace the whole renderer (blank app, lost session). Catch that via
  // will-navigate: anything that isn't our own file:// document is sent to the
  // real browser and the in-window navigation is cancelled.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (url.startsWith('file://')) return;   // our own app shell — allow
    event.preventDefault();
    shell.openExternal(url);
  });
}

// The notch HUD: a frameless, transparent, non-activating window pinned
// flush to the top-center of the primary display (over/around the notch),
// raised above the menu bar. Compact = a bar extending the notch; expanded
// = a glass card. Sizes from notchMetrics(); resized on demand.
// Widths follow BetterBot (fixed; the black square-top bar merges with the
// notch regardless). idle = invisible footprint sized to the real notch;
// active = wider so the count shows beside it; expanded = glass card.
const NOTCH_RADIUS = 14;       // bottom corners, matches BetterBot
const IDLE_CLICK_LIP = 16;     // transparent clickable strip below the notch when idle
const NOTCH = {
  active:   { w: 300 },
  expanded: { w: 360, h: 320 },
};

// Exact notch geometry from macOS (safeAreaInsets + auxiliary areas) via the
// native helper — Electron's screen API can't see the notch. Cached.
let _notch = null;
function notchMetrics() {
  if (_notch) return _notch;
  const { screen } = require('electron');
  const d = screen.getPrimaryDisplay();
  let notchHeight = Math.max(d.workArea.y - d.bounds.y, 0) || 32;
  let notchWidth = 200, hasNotch = false;
  try {
    const bin = app.isPackaged
      ? path.join(process.resourcesPath, 'notch-metrics')
      : path.join(__dirname, 'build', 'notch-metrics');
    const out = require('node:child_process').execFileSync(bin, [], { timeout: 3000 }).toString();
    const m = JSON.parse(out);
    if (m.notchHeight > 0) notchHeight = m.notchHeight;
    if (m.hasNotch && m.notchWidth > 0) { notchWidth = m.notchWidth; hasNotch = true; }
  } catch { /* fall back to the workArea inset + 200 */ }
  _notch = { display: d, notchHeight, notchWidth, hasNotch };
  return _notch;
}

function notchSize(mode) {
  const { notchHeight, notchWidth } = notchMetrics();
  if (mode === 'expanded') return { w: NOTCH.expanded.w, h: NOTCH.expanded.h };
  if (mode === 'active')   return { w: notchWidth + 160, h: notchHeight + 8 };  // shoulders beside the notch
  // idle: invisible, but extend a small clickable lip BELOW the camera so a
  // click "on the notch" lands on real screen (the camera housing itself is
  // hardware and can't receive clicks). Width stays = the notch so we never
  // sit over adjacent menu-bar items.
  return { w: notchWidth, h: notchHeight + IDLE_CLICK_LIP };
}

function positionNotch(mode) {
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  const { display } = notchMetrics();
  const { w, h } = notchSize(mode);
  const x = Math.round(display.bounds.x + display.bounds.width / 2 - w / 2);
  const y = display.bounds.y;   // absolute top — flush, over the notch
  overlayWindow.setBounds({ x, y, width: w, height: h });
  // Interactive in every mode — including idle, so clicking the (invisible)
  // notch region opens the HUD card. The idle footprint is only the notch
  // width + a small lip, sitting over the camera housing where there are no
  // menu-bar items, so it doesn't eat meaningful clicks.
  overlayWindow.setIgnoreMouseEvents(false);
}

function createOverlayWindow() {
  const init = notchSize('idle');
  overlayWindow = new BrowserWindow({
    width: init.w,
    height: init.h,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    focusable: false,
    hasShadow: false,
    fullscreenable: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  // Above the menu bar, on every space incl. fullscreen.
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWindow.loadFile(path.join(__dirname, 'overlay', 'index.html'));
  overlayWindow.on('closed', () => { overlayWindow = null; });
  positionNotch('idle');
}

ipcMain.handle('sunday:notch-metrics', () => ({ notchHeight: notchMetrics().notchHeight }));
ipcMain.on('sunday:notch-mode', (_evt, mode) => positionNotch(['idle', 'active', 'expanded'].includes(mode) ? mode : 'idle'));

ipcMain.handle('sunday:config', () => {
  const { daemonHttp, daemonWs, daemonToken } = resolveDaemon();
  // localHttp/localWs: this profile's own daemon address (per-user port) —
  // what onboarding should use for the "On this Mac" choice.
  return { daemonHttp, daemonWs, daemonToken,
           localHttp: LOCAL_HTTP, localWs: LOCAL_WS };
});

// Save the OpenRouter key to ~/.sunday/credentials.env (the daemon's
// credential store), then restart the embedded daemon so it loads it.
// Local-only — same machine writing a 0600 file.
ipcMain.handle('sunday:set-openrouter-key', async (_evt, key) => {
  const k = String(key || '').trim();
  if (!k) return { ok: false, error: 'empty key' };
  try {
    const dir = path.join(os.homedir(), '.sunday');
    fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, 'credentials.env');
    let lines = [];
    try { lines = fs.readFileSync(file, 'utf8').split('\n').filter((l) => l.trim() && !l.startsWith('OPENROUTER_API_KEY=')); } catch {}
    lines.push(`OPENROUTER_API_KEY=${k}`);
    fs.writeFileSync(file, lines.join('\n') + '\n', { mode: 0o600 });
    // Restart the local daemon so it reloads credentials (launchd-aware).
    await restartLocalDaemon();
    return { ok: true };
  } catch (e) { return { ok: false, error: e?.message || String(e) }; }
});

// The local daemon's auth token (read from its file). Used by onboarding to
// persist the token into prefs for a local install.
ipcMain.handle('sunday:local-token', () => {
  try {
    const p = path.join(os.homedir(), '.sunday', 'auth.token');
    return { token: fs.existsSync(p) ? fs.readFileSync(p, 'utf8').trim() : '' };
  } catch { return { token: '' }; }
});

ipcMain.handle('sunday:finish-onboarding', (_evt, { daemonHttp, daemonWs, label, daemonToken }) => {
  savePrefs({ daemonHttp, daemonWs, daemonToken: daemonToken || '', label, onboarded: true });
  if (!mainWindow) createMainWindow();
  if (onboardingWindow && !onboardingWindow.isDestroyed()) onboardingWindow.close();
  rebuildTrayMenu();
  return true;
});

ipcMain.handle('sunday:save-connection', (_evt, { daemonHttp, daemonWs, daemonToken }) => {
  savePrefs({ daemonHttp, daemonWs, daemonToken: daemonToken ?? loadPrefs().daemonToken ?? '' });
  // Reload the main window so it reconnects to the new daemon URL
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
  rebuildTrayMenu();
  return true;
});

// ── run mode: Cloud (remote daemon) ↔ Local (bundled daemon on this Mac) ──
// A toggle so you never type an IP. Local mode runs the bundled daemon and can
// use Codex (it reads your ~/.codex login). The cloud URL is remembered so you
// can flip back.
const LOCAL_HTTP = `http://127.0.0.1:${LOCAL_PORT}`;
const LOCAL_WS = `ws://127.0.0.1:${LOCAL_PORT}/v1/ws`;
function localDaemonToken() {
  try { return fs.readFileSync(path.join(os.homedir(), '.sunday', 'auth.token'), 'utf8').trim(); } catch { return ''; }
}

ipcMain.handle('sunday:run-mode', () => {
  const prefs = loadPrefs();
  return { local: isLocalDaemon(), role: resolveRole(), alwaysOn: serverDaemonInstalled(), cloudHttp: prefs.cloudDaemonHttp || (isLocalDaemon() ? '' : prefs.daemonHttp) || '' };
});

ipcMain.handle('sunday:set-run-mode', async (_evt, mode) => {
  const prefs = loadPrefs();
  if (mode === 'local') {
    if (!isLocalDaemon()) savePrefs({ cloudDaemonHttp: prefs.daemonHttp, cloudDaemonWs: prefs.daemonWs, cloudDaemonToken: prefs.daemonToken });
    savePrefs({ daemonHttp: LOCAL_HTTP, daemonWs: LOCAL_WS });
    const up = await startEmbeddedDaemon();
    if (!up) {
      // Don't strand the user on a "local" brain that never came up — revert so
      // the UI keeps pointing at the working cloud daemon instead of half-switching.
      // Also tear down any launchd agent a failed install left half-written.
      removeServerDaemon();
      savePrefs({ daemonHttp: prefs.daemonHttp, daemonWs: prefs.daemonWs, daemonToken: prefs.daemonToken });
      return { ok: false, error: "the local brain didn't start on this Mac — still on cloud" };
    }
    // The local brain IS the server — record the role explicitly so the rest of
    // the app (tray, settings, future always-on launchd) reads it directly.
    savePrefs({ daemonToken: localDaemonToken(), role: 'server' });
  } else {
    const ch = prefs.cloudDaemonHttp, cw = prefs.cloudDaemonWs;
    if (!ch) return { ok: false, error: 'no cloud daemon saved to switch back to' };
    savePrefs({ daemonHttp: ch, daemonWs: cw, daemonToken: prefs.cloudDaemonToken || prefs.daemonToken || '', role: 'satellite' });
    stopEmbeddedDaemon();
    removeServerDaemon();   // this Mac is no longer the server — stop the always-on daemon
  }
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
  rebuildTrayMenu();
  return { ok: true, local: mode === 'local' };
});

// Copy the current (cloud) daemon's data down to this Mac, then switch to
// local — so you keep your whole history + memory and gain Codex.
ipcMain.handle('sunday:migrate-to-local', async () => {
  if (isLocalDaemon()) return { ok: false, error: 'already running locally' };
  const { daemonHttp, daemonWs } = resolveDaemon();
  try {
    const home = path.join(os.homedir(), '.sunday');
    fs.mkdirSync(home, { recursive: true });
    const listRes = await fetch(`${daemonHttp}/v1/export`, { headers: _bearer() });
    if (!listRes.ok) return { ok: false, error: `export list failed: HTTP ${listRes.status}` };
    const { files } = await listRes.json();
    const backup = path.join(home, `pre-migrate-${Date.now()}`);
    for (const f of files) {
      const r = await fetch(`${daemonHttp}/v1/export?file=${encodeURIComponent(f)}`, { headers: _bearer() });
      if (!r.ok) continue;
      const buf = Buffer.from(await r.arrayBuffer());
      const dst = path.join(home, f);
      // back up any existing local db + clear stale WAL/SHM so the copy wins
      if (fs.existsSync(dst)) { fs.mkdirSync(backup, { recursive: true }); try { fs.renameSync(dst, path.join(backup, f)); } catch {} }
      for (const suf of ['-wal', '-shm']) { try { fs.unlinkSync(dst + suf); } catch {} }
      fs.writeFileSync(dst, buf);
    }
    savePrefs({ cloudDaemonHttp: daemonHttp, cloudDaemonWs: daemonWs, cloudDaemonToken: loadPrefs().daemonToken,
                daemonHttp: LOCAL_HTTP, daemonWs: LOCAL_WS, role: 'server' });
    await startEmbeddedDaemon();
    savePrefs({ daemonToken: localDaemonToken() });
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.reload();
    rebuildTrayMenu();
    return { ok: true, files };
  } catch (e) { return { ok: false, error: e?.message || String(e) }; }
});

ipcMain.handle('sunday:open-settings', () => {
  switchToView('settings');
  return true;
});

// Settings, Memory, and Chat are tabs in the one window now — focus it and
// tell the renderer which tab to show.
function switchToView(name) {
  if (!mainWindow) createMainWindow();
  else { mainWindow.show(); mainWindow.focus(); }
  setTimeout(() => mainWindow?.webContents.send('sunday:switch-view', name), 120);
}

// Check whether the satellite would be able to read iMessage history.
// We can't ask macOS directly — instead probe a path that requires Full
// Disk Access. Returns true when readable (i.e. FDA granted to whichever
// process the satellite runs under).
ipcMain.handle('sunday:check-fda', async () => {
  const chatDb = path.join(require('node:os').homedir(), 'Library', 'Messages', 'chat.db');
  try {
    // fs.accessSync with R_OK throws on protected paths even when the
    // file exists. On non-macOS or missing chat.db we return false.
    fs.accessSync(chatDb, fs.constants.R_OK);
    return true;
  } catch {
    return false;
  }
});

// Open Privacy & Security → Full Disk Access AND reveal Sunday.app in
// Finder so the user can drag the app itself into the FDA list — same
// flow Perplexity Comet uses for their permissions.
//
// macOS grants FDA per-process. Sunday.app's child processes (including
// any embedded satellite we spawn from here) inherit the grant. Until
// we embed the satellite (next slice — kills the "FDA on python3" path
// entirely), users still need to grant FDA to the satellite Python
// binary separately if they're running one. For the *app's own*
// iMessage reads (when we embed), this is the right pointer.
ipcMain.handle('sunday:open-fda-settings', async () => {
  // 1. Open the FDA pane via Apple's deep-link URL scheme.
  await shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles');

  // 2. Reveal Sunday.app itself in Finder so the user can drag it into
  //    the FDA list. app.getAppPath() returns the asar / app root;
  //    walking up to the .app bundle is one step.
  const appBundle = path.dirname(path.dirname(path.dirname(app.getAppPath())));
  // For dev (running `npm start`), the .app bundle doesn't exist — fall
  // back to revealing the project folder.
  if (appBundle.endsWith('.app')) {
    shell.showItemInFolder(appBundle);
    return { revealedPath: appBundle };
  }
  shell.openPath(require('node:os').homedir() + '/Applications');
  return { revealedPath: null };
});

// Read a Rewind frame off disk and hand it back as a data URL. The frames
// live under ~/.sunday/rewind on this same Mac (the satellite is local);
// reading via IPC sidesteps file:// subresource restrictions and lets us
// constrain access to the rewind directory.
ipcMain.handle('sunday:rewind-image', async (_evt, p) => {
  try {
    const dir = path.join(require('node:os').homedir(), '.sunday', 'rewind');
    const resolved = path.resolve(String(p || ''));
    if (!resolved.startsWith(dir)) return null;
    const buf = fs.readFileSync(resolved);
    return `data:image/png;base64,${buf.toString('base64')}`;
  } catch {
    return null;
  }
});

// Read a card's baked timelapse MP4 and hand it back as a data URL for the
// detail-pane <video>. Same rewind-dir sandbox as frame images; the clips live
// under ~/.sunday/rewind/evidence and persist after their source frames prune.
ipcMain.handle('sunday:timeline-video', async (_evt, p) => {
  try {
    const dir = path.join(require('node:os').homedir(), '.sunday', 'rewind');
    const resolved = path.resolve(String(p || ''));
    if (!resolved.startsWith(dir) || !resolved.endsWith('.mp4')) return null;
    const buf = fs.readFileSync(resolved);
    return `data:video/mp4;base64,${buf.toString('base64')}`;
  } catch {
    return null;
  }
});

// Open the Accessibility pane. Calling isTrustedAccessibilityClient(true)
// also asks macOS to register Sunday in the list with the system prompt.
// Status is read separately via sunday:permissions-status — never inferred
// from this handler's return value.
ipcMain.handle('sunday:request-control', async () => {
  try { systemPreferences.isTrustedAccessibilityClient(true); } catch {}
  shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility');
  return { ok: true };
});

ipcMain.handle('sunday:open-external', (_evt, url) => {
  try { shell.openExternal(String(url)); return true; } catch { return false; }
});

// Reveal the bundled Cockpit extension folder in Finder so the user can drag
// it onto chrome://extensions. Packaged: it ships under Resources/extension;
// dev: it lives at repo-root ../extension relative to this file.
ipcMain.handle('sunday:reveal-extension', () => {
  try {
    const dir = app.isPackaged
      ? path.join(process.resourcesPath, 'extension')
      : path.join(__dirname, '..', 'extension');
    shell.showItemInFolder(dir);
    return { ok: true, path: dir };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// Open chrome://extensions in the user's Chrome. `open -a Chrome chrome://…`
// is silently swallowed (Chrome refuses chrome:// URLs from the OS), but the
// AppleScript tab API sets the URL through Chrome's own scripting interface,
// which is allowed. First use triggers the one-time "Sunday wants to control
// Google Chrome" Automation prompt.
ipcMain.handle('sunday:open-chrome-extensions', () => new Promise((resolve) => {
  const script = 'tell application "Google Chrome"\n'
    + 'if (count of windows) = 0 then make new window\n'
    + 'set URL of active tab of front window to "chrome://extensions/"\n'
    + 'activate\n'
    + 'end tell';
  require('node:child_process').execFile('osascript', ['-e', script], { timeout: 15000 }, (err) => {
    resolve(err ? { ok: false, error: String(err.message || err) } : { ok: true });
  });
}));

ipcMain.on('sunday:overlay-state', (_evt, state) => {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send('sunday:overlay-state', state);
  }
});

function createOnboardingWindow() {
  onboardingWindow = new BrowserWindow({
    width: 720,
    height: 620,
    minWidth: 540,
    minHeight: 480,
    backgroundColor: windowBg(),
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 14, y: 14 },
    resizable: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  onboardingWindow.loadFile(path.join(__dirname, 'renderer', 'onboarding.html'));
  onboardingWindow.on('closed', () => { onboardingWindow = null; });
}

function createTray() {
  // Monochrome sun template — macOS tints it to match the menu bar theme
  // (black on light, white on dark). Shipped as 18px + @2x in renderer/ so
  // Electron picks the retina rep automatically. (Was an inline data URL,
  // but that base64 was a corrupt PNG → Electron drew an empty/invisible
  // icon, which is why nothing showed in the menu bar.)
  const icon = nativeImage.createFromPath(path.join(__dirname, 'renderer', 'trayTemplate.png'));
  if (!icon.isEmpty()) icon.setTemplateImage(true);
  tray = new Tray(icon.isEmpty() ? nativeImage.createEmpty() : icon);
  tray.setToolTip('Sunday');
  rebuildTrayMenu();
  tray.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) mainWindow.focus(); else mainWindow.show();
    } else if (!onboardingWindow) {
      createMainWindow();
    }
  });
}

// Live agent indicator IN THE MENU BAR: poll the daemon and show the running
// sub-agent count as text next to the sun icon (e.g. "☀ 2"). This is the
// reliable home for ambient status — the notch overlay can't read the real
// notch geometry from Electron, so the menu bar carries the live count.
let _trayStatusTimer = null;
function _fmtMins(s) { const m = Math.max(0, Math.round(s / 60)); return m >= 60 ? `${Math.floor(m / 60)}h${(m % 60) ? (m % 60) + 'm' : ''}` : `${m}m`; }
function _clockOf(ts) { try { return new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }); } catch { return ''; } }
function startTrayStatus() {
  const tick = async () => {
    if (!tray || tray.isDestroyed?.()) return;
    const { daemonHttp } = resolveDaemon();
    let n = 0, block = null;
    try {
      const res = await fetch(`${daemonHttp}/v1/status`, { signal: AbortSignal.timeout(2500), headers: { ..._bearer() } });
      if (res.ok) { const d = await res.json(); n = Array.isArray(d.agents) ? d.agents.length : 0; }
    } catch { return; /* daemon down — leave title as-is */ }
    // The timeblock is the "live contract" — it takes priority in the menu bar.
    try {
      const r = await fetch(`${daemonHttp}/v1/timeline/current-block`, { signal: AbortSignal.timeout(2500), headers: { ..._bearer() } });
      if (r.ok) block = await r.json();
    } catch { /* blocks unavailable — fall back to agent count */ }

    const cur = block && block.current;
    const nxt = block && block.next;
    if (cur) {
      const left = _fmtMins(block.ends_in_s || 0);
      const short = (cur.label || 'Block').slice(0, 22);
      const drift = cur.drift || {};
      const warn = drift.on_track === false ? ' ⚠' : '';
      tray.setTitle(` ◉ ${short} ${left}${warn}`);
      tray.setToolTip(
        `Now: ${cur.label} · ${left} left`
        + (drift.note ? `\n${drift.on_track === false ? '⚠' : '✓'} ${drift.note}` : '')
        + (nxt ? `\nNext: ${nxt.label} at ${_clockOf(nxt.start_ts)}` : '')
        + (n > 0 ? `\n${n} agent${n > 1 ? 's' : ''} working` : '')
      );
    } else if (nxt && (block.next_in_s || 0) <= 45 * 60) {
      tray.setTitle(` ○ ${(nxt.label || 'Block').slice(0, 18)} in ${_fmtMins(block.next_in_s)}`);
      tray.setToolTip(`Next: ${nxt.label} at ${_clockOf(nxt.start_ts)}`);
    } else {
      tray.setTitle(n > 0 ? ` ${n}` : '');
      tray.setToolTip(n > 0 ? `Sunday — ${n} agent${n > 1 ? 's' : ''} working` : 'Sunday');
    }
  };
  tick();
  _trayStatusTimer = setInterval(tick, 2000);
}

// Live update state — the tray menu shows different items + labels based on
// where autoUpdater is. Set by the autoUpdater event handlers; consumed in
// rebuildTrayMenu so the menu reflects the truth.
let _updateState = { phase: 'idle', message: '', version: null, percent: 0, current: app.getVersion() };
function setUpdateState(patch) {
  _updateState = { ..._updateState, ...patch };
  if (tray) rebuildTrayMenu();
  // Mirror into any open window so the Settings → Updates panel can react.
  if (mainWindow && !mainWindow.isDestroyed()) {
    try { mainWindow.webContents.send('sunday:update-state', _updateState); } catch {}
  }
}

function updateMenuItem() {
  const s = _updateState;
  switch (s.phase) {
    case 'checking':
      return { label: 'Checking for updates…', enabled: false };
    case 'available':
      return { label: `Downloading ${s.version}…`, enabled: false };
    case 'downloading':
      return { label: `Downloading ${s.version}… ${Math.round(s.percent || 0)}%`, enabled: false };
    case 'downloaded':
      return { label: `Restart to update to ${s.version}`, click: () => {
        try { autoUpdater.quitAndInstall(); } catch (e) { console.warn(e); }
      }};
    case 'none':
      return { label: 'Sunday is up to date', enabled: false };
    case 'error':
      return { label: `Update check failed${s.message ? ': ' + s.message : ''}`, enabled: false };
    case 'idle':
    default:
      return { label: 'Check for updates…', click: () => {
        setUpdateState({ phase: 'checking', message: '' });
        autoUpdater.checkForUpdates().catch((e) => {
          setUpdateState({ phase: 'error', message: e?.message || String(e) });
        });
      }};
  }
}

// Start (or stop) a meeting straight from the menu bar. getDisplayMedia needs
// transient activation, which a tray click doesn't give the renderer — so we
// run the renderer entrypoint via executeJavaScript with userGesture=true,
// which supplies it. Brings the window forward so the recording state is visible.
function triggerTrayMeeting() {
  if (!mainWindow) { createMainWindow(); }
  try { mainWindow.show(); mainWindow.focus(); } catch { /* window gone */ }
  switchToView('memory');   // surface the Meetings tab so the state is visible
  const run = () => {
    mainWindow.webContents
      .executeJavaScript('window.__sundayTrayMeeting && window.__sundayTrayMeeting()', true)
      .catch(() => {});
  };
  if (mainWindow.webContents.isLoading()) mainWindow.webContents.once('did-finish-load', run);
  else run();
}

function rebuildTrayMenu() {
  if (!tray) return;
  const { daemonHttp, onboarded } = resolveDaemon();
  const prefs = loadPrefs();
  const version = app.getVersion();
  const role = resolveRole();
  const roleLine = role === 'server'
    ? (serverDaemonInstalled() ? 'Server · this Mac is the brain · always-on' : 'Server · this Mac is the brain')
    : `Satellite · ${prefs.label || daemonHttp}`;
  const menu = Menu.buildFromTemplate([
    { label: `Sunday ${version}`, enabled: false },
    { label: roleLine, enabled: false },
    { type: 'separator' },
    { label: 'Chat',     accelerator: 'Command+1', click: () => switchToView('chat') },
    { label: 'Memory',   accelerator: 'Command+2', click: () => switchToView('memory') },
    { label: 'Settings…', accelerator: 'Command+,', click: () => switchToView('settings') },
    { type: 'separator' },
    { label: meetingRecording ? '■  Stop meeting' : '●  Start a meeting',
      click: () => triggerTrayMeeting() },
    { type: 'separator' },
    { label: notchHudChild ? 'Hide notch HUD' : 'Show notch HUD', click: () => {
        if (notchHudChild) { stopNotchHud(); savePrefs({ hud: false }); }
        else { startNotchHud(); savePrefs({ hud: true }); }
        rebuildTrayMenu();
    }},
    { type: 'separator' },
    updateMenuItem(),
    { type: 'separator' },
    { label: 'Reconfigure (re-run onboarding)…', click: () => {
        savePrefs({ onboarded: false });
        if (mainWindow) mainWindow.close();
        if (!onboardingWindow) createOnboardingWindow();
    }},
    { type: 'separator' },
    { label: 'Quit Sunday',  role: 'quit' },
  ]);
  tray.setContextMenu(menu);
}

app.whenReady().then(() => {
  // Meeting capture: hand getDisplayMedia a screen source + system-audio
  // loopback so the capture window records the other side of the call. This
  // is the whole fix — the audio is attributed to Sunday (which has Screen
  // Recording), not a detached helper that gets silence.
  const { session: electronSession } = require('electron');
  electronSession.defaultSession.setDisplayMediaRequestHandler((request, callback) => {
    desktopCapturer.getSources({ types: ['screen'] }).then((sources) => {
      callback({ video: sources[0], audio: 'loopback' });
    }).catch(() => callback({}));
  }, { useSystemPicker: false });
  // Approve media + display-capture permission requests for our own renderer.
  electronSession.defaultSession.setPermissionRequestHandler((wc, permission, cb) => {
    cb(['media', 'display-capture', 'microphone', 'audioCapture'].includes(permission) ? true : true);
  });

  // Serve meeting recordings to the renderer's <audio> from ~/.sunday/meetings.
  const { protocol } = require('electron');
  protocol.handle?.('sunday-audio', async (req) => {
    try {
      const u = new URL(req.url);   // sunday-audio://<cid>/<file>
      const cid = u.hostname;
      const file = decodeURIComponent(u.pathname.replace(/^\//, ''));
      if (!/^[\w.-]+$/.test(file)) return new Response('bad', { status: 400 });
      const p = path.join(os.homedir(), '.sunday', 'meetings', cid, file);
      const data = fs.readFileSync(p);
      return new Response(data, { headers: { 'Content-Type': 'audio/wav' } });
    } catch { return new Response('not found', { status: 404 }); }
  });

  // Spawn the embedded daemon first thing (fire-and-forget) so it's coming
  // up while the UI loads. Local installs get the brain with no terminal;
  // remote daemons skip this.
  startEmbeddedDaemon().catch(() => {});

  const { onboarded } = resolveDaemon();
  if (!onboarded) {
    createOnboardingWindow();
  } else {
    createMainWindow();
    // The notch HUD is a NATIVE Swift helper (the only way to read + draw the
    // real notch — Electron can't). It renders at the notch on the built-in
    // display and shows nothing when there's no notch. On by default; toggle
    // from the tray. Opt out with prefs.hud=false.
    if (loadPrefs().hud !== false) startNotchHud();
    // Argus is opt-in (off by default); bring it up if the nerd left it on.
    if (loadPrefs().argus) startArgus();
  }
  createTray();
  startTrayStatus();   // live sub-agent count in the menu bar
  Menu.setApplicationMenu(null);

  // Auto-update. Reads the Sparkle-compatible feed at the publish URL,
  // downloads any new build in the background, and prompts the user to
  // restart when ready. Because the build is code-signed with a stable
  // Developer ID, the new build inherits ALL TCC grants (mic, screen
  // recording, etc.) — no re-prompt loop on update.
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true;
  autoUpdater.on('checking-for-update', () => setUpdateState({ phase: 'checking' }));
  autoUpdater.on('update-available',    (info) => setUpdateState({ phase: 'available', version: info.version, percent: 0 }));
  autoUpdater.on('update-not-available', () => setUpdateState({ phase: 'none' }));
  autoUpdater.on('download-progress',   (p) => setUpdateState({ phase: 'downloading', percent: p.percent || 0 }));
  autoUpdater.on('update-downloaded',   (info) => {
    setUpdateState({ phase: 'downloaded', version: info.version });
    if (tray) tray.setToolTip(`Sunday update ready (${info.version}) — restart to apply`);
  });
  autoUpdater.on('error', (e) => setUpdateState({ phase: 'error', message: e?.message || 'unknown' }));
  // Check at startup + every 4h.
  setTimeout(() => autoUpdater.checkForUpdatesAndNotify().catch(() => {}), 8000);
  setInterval(() => autoUpdater.checkForUpdatesAndNotify().catch(() => {}), 4 * 60 * 60 * 1000);

  // Own the satellite as a child process so its macOS TCC grants
  // (Screen Recording etc.) attribute to "Sunday", not a standalone
  // Python LaunchAgent. Only when onboarded + not explicitly disabled.
  const prefs = loadPrefs();
  // Auto-start the ambient observer if the user left it on last time.
  if (prefs.onboarded && prefs.observer === true) startObserver();

  // Auto-install local transcription in the background. Sunday's promise is
  // self-hosted personal AI — audio shouldn't be leaving the Mac by default.
  // OpenAI Whisper remains as silent fallback until local is ready.
  if (prefs.onboarded) {
    setTimeout(() => {
      if (!localTranscriptionStatus().ready) {
        installLocalTranscription(() => {}).catch(() => {});
      }
    }, 3000);
  }

  if (prefs.onboarded && prefs.embeddedSatellite !== false) {
    // Hand the satellite launcher the bundled daemon binary path — on a
    // packaged install (no repo/venv) it runs `<bin> satellite` so a device
    // actually connects. No-op on dev machines, which prefer their venv.
    // Also pass the resolved daemon token so the satellite can authenticate
    // (resolveDaemon reads the local token file, or prefs for a remote daemon).
    satellite.start({
      ...prefs,
      daemonToken: resolveDaemon().daemonToken,
      bundledDaemonBin: bundledDaemonBinary(),
    });
  }
});

// Probe Full Disk Access by trying to read the protected Messages chat.db.
// macOS doesn't expose an API for this; the read-attempt is the canonical
// test (it's what every privacy-checker app does).
function fullDiskStatus() {
  try {
    const p = path.join(os.homedir(), 'Library', 'Messages', 'chat.db');
    if (!fs.existsSync(p)) return 'not-determined';  // no Messages history yet
    fs.openSync(p, 'r');   // throws EPERM/ENOENT when blocked by TCC
    return 'granted';
  } catch (e) {
    return e?.code === 'EPERM' || e?.code === 'EACCES' ? 'denied' : 'not-determined';
  }
}

// Permission status — read straight from macOS. Authoritative; never guesses.
// Returns each ∈ 'granted' | 'denied' | 'not-determined'.
ipcMain.handle('sunday:update-state', () => _updateState);
ipcMain.handle('sunday:update-check', () => {
  setUpdateState({ phase: 'checking', message: '' });
  return autoUpdater.checkForUpdates()
    .then(() => ({ ok: true }))
    .catch((e) => { setUpdateState({ phase: 'error', message: e?.message || String(e) }); return { ok: false }; });
});
ipcMain.handle('sunday:update-restart', () => {
  try { autoUpdater.quitAndInstall(); } catch (e) { return { error: e?.message || String(e) }; }
  return { ok: true };
});

ipcMain.handle('sunday:permissions-status', () => {
  const mic = systemPreferences.getMediaAccessStatus
    ? systemPreferences.getMediaAccessStatus('microphone')
    : 'not-determined';
  const screen = systemPreferences.getMediaAccessStatus
    ? systemPreferences.getMediaAccessStatus('screen')
    : 'not-determined';
  const control = systemPreferences.isTrustedAccessibilityClient
    ? (systemPreferences.isTrustedAccessibilityClient(false) ? 'granted' : 'not-determined')
    : 'not-determined';
  const fullDisk = fullDiskStatus();
  return { microphone: mic, screen, control, fullDisk };
});

// Microphone — askForMediaAccess shows the system prompt the first time and
// returns true/false thereafter. Idempotent.
ipcMain.handle('sunday:request-microphone', async () => {
  try { await systemPreferences.askForMediaAccess('microphone'); } catch {}
  // Open the pane in case the user previously denied — the prompt won't fire again.
  shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone');
  return { ok: true };
});

// Full Disk Access — no programmatic prompt exists; just open the pane.
ipcMain.handle('sunday:request-fulldisk', async () => {
  shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles');
  return { ok: true };
});

// Two separate actions — prompt the system to register Sunday (which puts it
// in the right Privacy pane), then open that pane so the user can toggle it.
// Status reads via sunday:permissions-status above; never via these handlers.
ipcMain.handle('sunday:request-screen', async () => {
  try {
    // Forces macOS to register Sunday under Screen Recording. The actual
    // capture call may fail (we ignore it); the side-effect of registration
    // is the whole point.
    await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 1, height: 1 } });
  } catch { /* registration succeeded even if capture didn't — that's fine */ }
  shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
  return { ok: true };
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  satellite.stop();
  stopNotchHud();
  stopArgus();
  stopObserver();
  stopEmbeddedDaemon();
  meetingRecording = false;
});

// ── ambient observer ──────────────────────────────────────────────────────
// Capture runs INSIDE Sunday (a hidden BrowserWindow using getUserMedia), so
// the macOS mic prompt is attributed to "Sunday" — not a detached Python
// child that the user (rightly) refuses. The hidden window records ~30s
// chunks, hands them to main, main transcribes via Whisper and POSTs the
// transcript to the daemon's /v1/observer/tick (where the brain lives).
let captureWindow = null;
let observerState = { active: false, error: null, lastChunkAt: null };

function micStatus() {
  try { return systemPreferences.getMediaAccessStatus('microphone'); }
  catch { return 'unknown'; }
}

async function startObserver() {
  if (captureWindow) return;
  // Request mic the honest way — this prompt says "Sunday", and the grant
  // (which the app already has the entitlement for) actually applies.
  let status = micStatus();
  if (status === 'not-determined') {
    try { await systemPreferences.askForMediaAccess('microphone'); } catch { /* user dismissed */ }
    status = micStatus();
  }
  if (status !== 'granted') {
    observerState = { active: false, error: `mic-${status}`, lastChunkAt: null };
    return;
  }
  captureWindow = new BrowserWindow({
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload-capture.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,   // keep capturing when not focused / tray
    },
  });
  captureWindow.loadFile(path.join(__dirname, 'renderer', 'capture.html'));
  captureWindow.on('closed', () => { captureWindow = null; observerState.active = false; });
}

function stopObserver() {
  try { captureWindow?.close(); } catch { /* already gone */ }
  captureWindow = null;
  observerState = { active: false, error: null, lastChunkAt: null };
}

// ("Hey Sunday" wake word removed — superseded by realtime voice mode.)

// Read OPENAI key the same way the daemon credential store does: env →
// ~/.sunday/credentials.env → keychain. Main-process only; never the renderer.
function readOpenAIKey() {
  if (process.env.OPENAI_API_KEY) return process.env.OPENAI_API_KEY;
  try {
    const envPath = path.join(os.homedir(), '.sunday', 'credentials.env');
    const txt = fs.readFileSync(envPath, 'utf8');
    const m = txt.match(/^OPENAI_API_KEY=(.+)$/m);
    if (m) return m[1].trim();
  } catch { /* fall through */ }
  try {
    return require('node:child_process')
      .execSync('security find-generic-password -s OPENAI_API_KEY -w', { encoding: 'utf8' })
      .trim();
  } catch { return null; }
}

// ── transcription: local whisper.cpp first (audio never leaves the Mac),
//    OpenAI Whisper as fallback when local isn't installed. ──
// Preferred → fallback. small.en gives noticeably cleaner transcripts than
// base.en (acronyms, partial words, low-volume speech) while still finishing
// well inside a 30s chunk on Apple Silicon. base.en stays as a fallback so
// existing installs keep working while small.en downloads in the background.
const WHISPER_DIR = path.join(os.homedir(), '.sunday', 'whisper');
const WHISPER_MODELS = {
  preferred:  { name: 'small.en', file: 'ggml-small.en.bin',
                url: 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin',
                approx_mb: 488 },
  fallback:   { name: 'base.en',  file: 'ggml-base.en.bin',
                url: 'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin',
                approx_mb: 148 },
};
function modelPath(modelDef) { return path.join(WHISPER_DIR, modelDef.file); }
function modelInstalled(modelDef) {
  try { return fs.statSync(modelPath(modelDef)).size > 1_000_000; } catch { return false; }
}
function activeModel() {
  if (modelInstalled(WHISPER_MODELS.preferred)) return WHISPER_MODELS.preferred;
  if (modelInstalled(WHISPER_MODELS.fallback))  return WHISPER_MODELS.fallback;
  return null;
}

const WHISPER_LOCAL = {
  bin:   ['/opt/homebrew/bin/whisper-cli', '/usr/local/bin/whisper-cli'].find(p => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; } }),
  ffmpeg: ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'].find(p => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; } }),
  // Resolved at each call so an upgrade from base→small picks up live
  // without restarting the app.
  get model() { return modelPath(activeModel() || WHISPER_MODELS.preferred); },
};
function localTranscriptionStatus() {
  const active = activeModel();
  const preferredReady = modelInstalled(WHISPER_MODELS.preferred);
  return {
    bin:    !!WHISPER_LOCAL.bin,
    ffmpeg: !!WHISPER_LOCAL.ffmpeg,
    model:  !!active,
    model_name: active ? active.name : null,
    upgrading: !preferredReady,   // running base.en while small.en downloads
    ready:  !!(WHISPER_LOCAL.bin && WHISPER_LOCAL.ffmpeg && active),
  };
}

// Log a transcription event to the daemon's cost meter. Local is recorded as
// $0/local; OpenAI is recorded with real duration so the meter stops lying.
async function logTranscriptionCost(provider, durationSeconds, latencyMs) {
  try {
    const { daemonHttp } = resolveDaemon();
    const active = activeModel();
    const model = provider === 'openai'
      ? 'whisper-1'
      : `whisper.cpp/${active ? active.name : 'unknown'}`;
    await fetch(`${daemonHttp}/v1/cost/log`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ..._bearer() },
      body: JSON.stringify({
        kind: 'audio', purpose: 'observer_tick', provider, model,
        audio_seconds: durationSeconds, latency_ms: latencyMs,
      }),
    });
  } catch { /* observability shouldn't break the tick */ }
}

async function transcribeLocal(webmBytes, durationSeconds) {
  const tmp = path.join(os.tmpdir(), `sunday-${Date.now()}`);
  const webm = `${tmp}.webm`, wav = `${tmp}.wav`;
  const t0 = Date.now();
  try {
    fs.writeFileSync(webm, Buffer.from(webmBytes));
    // ffmpeg: webm → 16kHz mono s16 WAV (whisper.cpp's preferred format).
    await new Promise((resolve, reject) => {
      const p = require('node:child_process').spawn(WHISPER_LOCAL.ffmpeg,
        ['-y', '-i', webm, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', wav],
        { stdio: 'ignore' });
      p.on('exit', (c) => c === 0 ? resolve() : reject(new Error(`ffmpeg exit ${c}`)));
      p.on('error', reject);
    });
    // whisper-cli: read WAV, print transcript to stdout via --no-prints --output-txt off.
    const out = await new Promise((resolve, reject) => {
      const chunks = [];
      const p = require('node:child_process').spawn(WHISPER_LOCAL.bin,
        ['-m', WHISPER_LOCAL.model, '-f', wav, '--no-timestamps', '--language', 'en', '-otxt', '-of', tmp],
        { stdio: ['ignore', 'pipe', 'ignore'] });
      p.stdout.on('data', (d) => chunks.push(d));
      p.on('exit', (c) => c === 0 ? resolve(Buffer.concat(chunks).toString('utf8')) : reject(new Error(`whisper exit ${c}`)));
      p.on('error', reject);
    });
    // whisper-cli writes the transcript to <tmp>.txt; prefer that since stdout
    // can carry status noise. Fall through to stdout if the file isn't there.
    let text = '';
    try { text = fs.readFileSync(`${tmp}.txt`, 'utf8'); } catch { text = out; }
    text = text.trim();
    await logTranscriptionCost('local', durationSeconds, Date.now() - t0);
    return text;
  } catch (err) {
    return null;
  } finally {
    for (const f of [webm, wav, `${tmp}.txt`]) { try { fs.unlinkSync(f); } catch {} }
  }
}

async function transcribeOpenAI(webmBytes, durationSeconds) {
  const key = readOpenAIKey();
  if (!key) return null;
  const t0 = Date.now();
  try {
    const form = new FormData();
    form.append('file', new Blob([webmBytes], { type: 'audio/webm' }), 'chunk.webm');
    form.append('model', 'whisper-1');
    const res = await fetch('https://api.openai.com/v1/audio/transcriptions', {
      method: 'POST',
      headers: { Authorization: `Bearer ${key}` },
      body: form,
    });
    if (!res.ok) return null;
    const data = await res.json();
    await logTranscriptionCost('openai', durationSeconds, Date.now() - t0);
    return (data.text || '').trim();
  } catch { return null; }
}

async function transcribeChunk(bytes, durationSeconds = 30) {
  // Local first — audio never leaves the Mac when available. Fall back only
  // if local isn't installed or fails on this specific chunk.
  if (localTranscriptionStatus().ready) {
    const local = await transcribeLocal(bytes, durationSeconds);
    if (local !== null && local !== '') return local;
    // If the local pipe failed (not just silence), let OpenAI take this one.
  }
  return await transcribeOpenAI(bytes, durationSeconds);
}

ipcMain.handle('sunday:transcription-status', () => localTranscriptionStatus());

// ── Meeting mode ────────────────────────────────────────────────────────
// Explicit, full-fidelity recording of a meeting: both sides (system audio +
// mic) to two tracks → transcribe each with timestamps → interleave with
// speaker labels → daemon summarizes (Granola-style) + stores + makes atoms.
// Meeting capture runs in a hidden Sunday window (getDisplayMedia for system
// audio, getUserMedia for mic) so both grants are the APP's own — the detached
// Swift recorder couldn't get the Screen Recording grant and captured silence.
let meetingWin = null;
let meetingDir = null;
let meetingStartedAt = null;
let meetingStreams = null;   // { system: WriteStream, mic: WriteStream }
let meetingStopPoll = null;
let meetingRecording = false;

async function setMeetingHud(recording) {
  try {
    const { daemonHttp } = resolveDaemon();
    await fetch(`${daemonHttp}/v1/meetings/hud`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ..._bearer() },
      body: JSON.stringify({ recording, since: recording ? meetingStartedAt / 1000 : null }),
    });
  } catch {}
}

// Capture happens in the MAIN window renderer (where the record-button click
// provides the user gesture getDisplayMedia requires). Main just owns the
// files + the finalize. beginMeeting sets up; chunks stream in; finalizeMeeting
// transcribes + summarizes.
function beginMeeting() {
  if (meetingRecording) return { ok: true, already: true };
  const id = `${Date.now()}`;
  meetingDir = path.join(os.homedir(), '.sunday', 'meetings', id);
  fs.mkdirSync(meetingDir, { recursive: true });
  meetingStartedAt = Date.now();
  meetingRecording = true;
  if (tray) rebuildTrayMenu();   // flip the tray item to "Stop meeting"
  meetingStreams = {
    system: fs.createWriteStream(path.join(meetingDir, 'system.webm')),
    mic: fs.createWriteStream(path.join(meetingDir, 'mic.webm')),
  };
  setMeetingHud(true);
  // Stop-request from the notch → tell the renderer to stop capturing.
  if (meetingStopPoll) clearInterval(meetingStopPoll);
  meetingStopPoll = setInterval(async () => {
    if (!meetingRecording) { clearInterval(meetingStopPoll); meetingStopPoll = null; return; }
    try {
      const { daemonHttp } = resolveDaemon();
      const s = await (await fetch(`${daemonHttp}/v1/status`, { headers: { ..._bearer() } })).json();
      if (s.meeting && s.meeting.stop_requested) {
        clearInterval(meetingStopPoll); meetingStopPoll = null;
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send('sunday:meeting-stop-now', {});
      }
    } catch {}
  }, 2000);
  return { ok: true, id };
}

// Chunks from the capturing renderer → append to the per-track webm files.
ipcMain.handle('sunday:meeting-chunk', (_evt, track, bytes) => {
  try { meetingStreams && meetingStreams[track] && meetingStreams[track].write(Buffer.from(bytes)); } catch {}
  return { ok: true };
});
ipcMain.handle('sunday:meeting-begin', () => beginMeeting());

// whisper-cli WITH timestamps → [{start, text}] for one wav.
async function transcribeTrackTimed(wav) {
  if (!localTranscriptionStatus().ready) return [];
  return new Promise((resolve) => {
    const out = [];
    const p = require('node:child_process').spawn(WHISPER_LOCAL.bin,
      ['-m', WHISPER_LOCAL.model, '-f', wav, '--language', 'en'],
      { stdio: ['ignore', 'pipe', 'ignore'] });
    let buf = '';
    p.stdout.on('data', (d) => { buf += d.toString(); });
    p.on('exit', () => {
      // Lines like: [00:00:01.200 --> 00:00:04.000]   some text
      const re = /\[(\d\d):(\d\d):(\d\d)\.\d+\s*-->.*?\]\s*(.*)/g;
      let m;
      while ((m = re.exec(buf)) !== null) {
        const start = (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
        const text = (m[4] || '').trim();
        if (text) out.push({ start, text });
      }
      resolve(out);
    });
    p.on('error', () => resolve([]));
  });
}

async function finalizeMeeting() {
  if (!meetingRecording || !meetingDir) { await setMeetingHud(false); return { ok: false, error: 'no meeting running' }; }
  const dir = meetingDir;
  const startedAt = meetingStartedAt;
  meetingRecording = false;
  if (tray) rebuildTrayMenu();   // flip the tray item back to "Start a meeting"
  // Close the webm streams (the renderer has already stopped sending chunks).
  try { meetingStreams.system.end(); meetingStreams.mic.end(); } catch {}
  await new Promise((r) => setTimeout(r, 500));
  meetingDir = null; meetingStreams = null;
  await setMeetingHud(false);

  // Convert each webm track → 16k mono wav for whisper.
  const webmToMono = async (src) => {
    if (!fs.existsSync(src) || fs.statSync(src).size < 1000) return null;
    const dst = src.replace(/\.webm$/, '-16k.wav');
    try {
      await new Promise((res, rej) => {
        const p = require('node:child_process').spawn(WHISPER_LOCAL.ffmpeg, ['-y', '-i', src, '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', dst], { stdio: 'ignore' });
        p.on('exit', (c) => c === 0 ? res() : rej(new Error('ffmpeg')));
        p.on('error', rej);
      });
      return dst;
    } catch { return null; }
  };

  const segs = [];
  const s = await webmToMono(path.join(dir, 'system.webm'));
  if (s) for (const seg of await transcribeTrackTimed(s)) segs.push({ ...seg, who: 'Others' });
  const m = await webmToMono(path.join(dir, 'mic.webm'));
  if (m) for (const seg of await transcribeTrackTimed(m)) segs.push({ ...seg, who: 'You' });
  segs.sort((a, b) => a.start - b.start);
  const transcript = segs.map((s) => `${s.who}: ${s.text}`).join('\n');

  if (!transcript.trim()) return { ok: false, error: 'no speech captured' };

  // Daemon summarizes + stores + makes atoms.
  try {
    const { daemonHttp } = resolveDaemon();
    const res = await fetch(`${daemonHttp}/v1/meetings/finalize`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ..._bearer() },
      body: JSON.stringify({ transcript, started_at: startedAt / 1000, ended_at: Date.now() / 1000 }),
    });
    const data = await res.json();
    if (!res.ok) {
      // 422 no_audio etc. — surface the daemon's detail, don't pretend it worked.
      return { ok: false, error: data.detail || data.error || `HTTP ${res.status}` };
    }
    // Link the recording to its conversation id so the meeting view can play
    // it back: rename the dir to <conversation_id> + mix the two tracks.
    try {
      const cid = data.conversation_id;
      if (cid) {
        const linkedDir = path.join(os.homedir(), '.sunday', 'meetings', String(cid));
        if (dir !== linkedDir) { try { fs.renameSync(dir, linkedDir); } catch {} }
        const sys = path.join(linkedDir, 'system.webm'), mic = path.join(linkedDir, 'mic.webm');
        const haveSys = fs.existsSync(sys), haveMic = fs.existsSync(mic);
        if (haveSys && haveMic) {
          await new Promise((res) => {
            const p = require('node:child_process').spawn(WHISPER_LOCAL.ffmpeg,
              ['-y', '-i', sys, '-i', mic, '-filter_complex', 'amix=inputs=2:duration=longest', path.join(linkedDir, 'mix.wav')],
              { stdio: 'ignore' });
            p.on('exit', res); p.on('error', res);
          });
        } else if (haveSys || haveMic) {
          await new Promise((res) => {
            const p = require('node:child_process').spawn(WHISPER_LOCAL.ffmpeg,
              ['-y', '-i', haveSys ? sys : mic, path.join(linkedDir, 'mix.wav')], { stdio: 'ignore' });
            p.on('exit', res); p.on('error', res);
          });
        }
      }
    } catch {}
    // Notify the notch the summary is ready.
    setMeetingDone(data.notes?.title || 'Meeting');
    return { ok: true, notes: data.notes, conversation_id: data.conversation_id };
  } catch (e) {
    return { ok: false, error: e?.message || String(e) };
  }
}

// Tell the notch a meeting summary is ready (it shows a toast).
async function setMeetingDone(title) {
  try {
    const { daemonHttp } = resolveDaemon();
    await fetch(`${daemonHttp}/v1/meetings/done`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', ..._bearer() },
      body: JSON.stringify({ title }),
    });
  } catch {}
}

ipcMain.handle('sunday:meeting-finalize-now', () => finalizeMeeting());
ipcMain.handle('sunday:meeting-state', () => ({ recording: meetingRecording, since: meetingStartedAt }));
// Playback URL for a meeting's recording, if it's still on disk. Returns a
// custom-scheme URL the renderer's <audio> can load (registered below).
ipcMain.handle('sunday:meeting-audio', (_evt, cid) => {
  try {
    const dir = path.join(os.homedir(), '.sunday', 'meetings', String(cid));
    for (const f of ['mix.wav', 'system.wav', 'mic.wav']) {
      if (fs.existsSync(path.join(dir, f))) return { url: `sunday-audio://${cid}/${f}` };
    }
  } catch {}
  return { url: null };
});

// One-click install of the local transcription stack. Two pieces:
//   1. whisper-cpp + ffmpeg via Homebrew (already installed; user has it).
//   2. The base.en model — straight HTTPS download from Hugging Face.
// Streams progress back to the renderer; no Terminal, no copy-paste.
let installInFlight = false;
async function installLocalTranscription(send) {
  if (installInFlight) return { error: 'install already running' };
  installInFlight = true;
  const log = (line) => { try { send && send({ line }); } catch {} };
  try {
    // brew location — same probe as the bin search.
    const brewBin = ['/opt/homebrew/bin/brew', '/usr/local/bin/brew'].find(p => {
      try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; }
    });

    // Step 1: whisper-cpp + ffmpeg via brew (if either is missing).
    const need = [];
    if (!WHISPER_LOCAL.bin)    need.push('whisper-cpp');
    if (!WHISPER_LOCAL.ffmpeg) need.push('ffmpeg');
    if (need.length) {
      if (!brewBin) {
        return { error: 'Homebrew not found. Install Homebrew first: https://brew.sh' };
      }
      log(`Installing ${need.join(' + ')} via Homebrew…`);
      await new Promise((resolve, reject) => {
        const p = require('node:child_process').spawn(brewBin, ['install', ...need], {
          env: { ...process.env, PATH: `${process.env.PATH || ''}:/opt/homebrew/bin:/usr/local/bin` },
        });
        p.stdout.on('data', (d) => log(d.toString().trimEnd()));
        p.stderr.on('data', (d) => log(d.toString().trimEnd()));
        p.on('exit', (c) => c === 0 ? resolve() : reject(new Error(`brew install exit ${c}`)));
        p.on('error', reject);
      });
      // Re-probe so the new bin paths are picked up for this process.
      WHISPER_LOCAL.bin    = ['/opt/homebrew/bin/whisper-cli', '/usr/local/bin/whisper-cli'].find(p => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; } });
      WHISPER_LOCAL.ffmpeg = ['/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg'].find(p => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; } });
    } else {
      log('whisper-cpp + ffmpeg already installed');
    }

    // Step 2: download the PREFERRED model (small.en, ~488MB). If base.en
    // already exists from an older install, leave it — transcription keeps
    // working off base.en during the small.en download window.
    const target = WHISPER_MODELS.preferred;
    if (!modelInstalled(target)) {
      fs.mkdirSync(WHISPER_DIR, { recursive: true });
      const dest = modelPath(target);
      const tmp = `${dest}.part`;
      log(`Downloading ${target.name} model (~${target.approx_mb}MB)…`);
      const res = await fetch(target.url);
      if (!res.ok || !res.body) throw new Error(`download failed: HTTP ${res.status}`);
      const total = Number(res.headers.get('content-length') || 0);
      let got = 0; let lastPct = -1;
      const out = fs.createWriteStream(tmp);
      const reader = res.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        out.write(value);
        got += value.length;
        if (total) {
          const pct = Math.floor((got / total) * 100);
          if (pct !== lastPct && pct % 5 === 0) { log(`  ${pct}% (${(got / 1e6).toFixed(1)} / ${(total / 1e6).toFixed(0)} MB)`); lastPct = pct; }
        }
      }
      await new Promise((resolve, reject) => out.end((e) => e ? reject(e) : resolve()));
      fs.renameSync(tmp, dest);
      log(`${target.name} installed. From the next tick onward, transcription uses the better model.`);
      // Clean up the now-obsolete fallback so disk doesn't grow forever.
      try {
        const fallback = modelPath(WHISPER_MODELS.fallback);
        if (fs.existsSync(fallback)) { fs.unlinkSync(fallback); log('Cleaned up old base.en (~148MB).'); }
      } catch { /* leave it; cleanup is cosmetic */ }
    } else {
      log(`${target.name} already installed.`);
    }

    log('✓ Local transcription ready.');
    return { ok: true, status: localTranscriptionStatus() };
  } catch (err) {
    log(`Install failed: ${err.message}`);
    return { error: err.message };
  } finally {
    installInFlight = false;
  }
}

ipcMain.handle('sunday:install-local-transcription', async (evt) => {
  return await installLocalTranscription((line) => evt.sender.send('sunday:install-log', line));
});

ipcMain.handle('sunday:observer-status', () => ({
  enabled: loadPrefs().observer === true,      // the on/off intent (what the toggle reflects)
  running: !!(captureWindow && observerState.active),   // capture confirmed live (status text)
  mic: micStatus(),
  error: observerState.error,
  lastChunkAt: observerState.lastChunkAt,
}));

ipcMain.handle('sunday:observer-set', async (_evt, on) => {
  if (on) { await startObserver(); savePrefs({ observer: true }); }
  else    { stopObserver();        savePrefs({ observer: false }); }
  return {
    enabled: !!on,
    running: !!(captureWindow && observerState.active),
    mic: micStatus(),
    error: observerState.error,
  };
});

// The capture window reports whether it actually got the mic stream.
ipcMain.on('sunday:observer-capture-state', (_evt, state) => {
  observerState.active = !!(state && state.active);
  observerState.error = (state && state.error) || null;
});

// (wake-word IPC removed — superseded by realtime voice mode.)

// A finished ~30s chunk arrives from the capture window → transcribe → tick.
ipcMain.handle('sunday:observer-chunk', async (_evt, bytes) => {
  observerState.lastChunkAt = Date.now();
  const transcript = await transcribeChunk(bytes);
  try {
    const { daemonHttp } = resolveDaemon();
    await fetch(`${daemonHttp}/v1/observer/tick`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._bearer() },
      body: JSON.stringify({ transcript: transcript || '', silent: !transcript }),
    });
  } catch { /* daemon unreachable; drop this tick */ }
  return { ok: true, transcribed: !!transcript };
});

// ── native notch HUD helper (Swift) ──────────────────────────────────────
// Renders the HUD at the real notch; draws nothing if the active display has
// no notch. Launched as a child so it's covered by Sunday's signature/TCC.
let notchHudChild = null;
function startNotchHud() {
  if (notchHudChild) return;
  try {
    const { daemonHttp, daemonToken } = resolveDaemon();
    const bin = app.isPackaged
      ? path.join(process.resourcesPath, 'NotchHUD.app', 'Contents', 'MacOS', 'notch-hud')
      : path.join(__dirname, 'build', 'NotchHUD.app', 'Contents', 'MacOS', 'notch-hud');
    // Pass the token as a 2nd arg so the notch can auth its status polls + WS.
    notchHudChild = require('node:child_process').spawn(bin, [daemonHttp, daemonToken || ''], { stdio: 'ignore' });
    notchHudChild.on('exit', () => { notchHudChild = null; });
  } catch { notchHudChild = null; }
}
function stopNotchHud() {
  try { notchHudChild?.kill(); } catch { /* already gone */ }
  notchHudChild = null;
}

// ── Argus: opt-in agent observability ("for nerds") ──────────────────────
// Argus is a zero-dependency Node app (dashboard + OTLP ingest on :4317, SQLite
// via node:sqlite). It's fetched fresh from mylesndavid/argus at BUILD time and
// bundled as a resource, so each release ships the latest. When the user turns
// it on we spawn it as its own process and the daemon ships traces to it via
// SUNDAY_ARGUS_URL. Off by default — 99% of users never run it.
const ARGUS_URL = 'http://127.0.0.1:4317';
let argusChild = null;

function argusEntry() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'argus', 'bin', 'argus.js')
    : path.join(__dirname, 'build', 'argus', 'bin', 'argus.js');
}
function argusAvailable() {
  try { return fs.existsSync(argusEntry()); } catch { return false; }
}

// Argus needs Node >= 22 (node:sqlite). Electron's own Node may be older, so
// find a real system node. Returns a path, or null if none qualifies.
function findNode() {
  const { execFileSync } = require('node:child_process');
  const candidates = [process.env.SUNDAY_NODE_BIN,
    '/opt/homebrew/bin/node', '/usr/local/bin/node', '/usr/bin/node',
    path.join(os.homedir(), '.volta/bin/node')].filter(Boolean);
  try { const p = execFileSync('/bin/sh', ['-lc', 'command -v node'], { encoding: 'utf8' }).trim(); if (p) candidates.push(p); } catch {}
  for (const bin of candidates) {
    try {
      const major = parseInt(execFileSync(bin, ['--version'], { encoding: 'utf8' }).trim().replace(/^v/, '').split('.')[0], 10);
      if (major >= 22) return bin;
    } catch { /* not this one */ }
  }
  return null;
}

function startArgus() {
  if (argusChild) return true;
  if (!argusAvailable()) { console.warn('argus: not bundled in this build'); return false; }
  const node = findNode();
  if (!node) { console.warn('argus: needs Node >= 22, none found'); return false; }
  try {
    argusChild = require('node:child_process').spawn(node, [argusEntry()], {
      cwd: path.dirname(path.dirname(argusEntry())),   // the argus/ root
      env: { ...process.env, PORT: '4317' },
      stdio: 'ignore', detached: false,
    });
    argusChild.on('exit', () => { argusChild = null; });
    argusChild.on('error', (e) => { console.warn('argus spawn error', e?.message); argusChild = null; });
    console.log('argus started on', ARGUS_URL);
    return true;
  } catch (e) { console.warn('argus spawn failed', e?.message); argusChild = null; return false; }
}
function stopArgus() {
  try { argusChild?.kill(); } catch { /* already gone */ }
  argusChild = null;
}

ipcMain.handle('sunday:argus-status', () => ({
  enabled: !!loadPrefs().argus, running: !!argusChild,
  available: argusAvailable(), nodeOk: !!findNode(), url: ARGUS_URL,
}));
ipcMain.handle('sunday:argus-set', async (_evt, enabled) => {
  enabled = !!enabled;
  if (enabled) {
    if (!argusAvailable()) return { ok: false, error: "Argus isn't bundled in this build." };
    if (!findNode()) return { ok: false, error: 'Argus needs Node 22+ on your PATH.' };
  }
  savePrefs({ argus: enabled });
  if (enabled) startArgus(); else stopArgus();
  // Restart the local daemon so it picks up (or drops) SUNDAY_ARGUS_URL —
  // launchd-aware, so a server's always-on daemon is reloaded, not pkill'd.
  await restartLocalDaemon();
  return { ok: true, running: !!argusChild };
});
ipcMain.handle('sunday:argus-open', () => { try { shell.openExternal(ARGUS_URL); return { ok: true }; } catch (e) { return { ok: false, error: String(e) }; } });

// Start-at-login. app.setLoginItemSettings registers via the same macOS
// Background Task Management database as System Settings → Login Items, so the
// toggle stays in sync both ways. openAsHidden launches her to the menu bar /
// background rather than popping a window in your face every boot.
ipcMain.handle('sunday:login-item-get', () => {
  try { return { ok: true, openAtLogin: !!app.getLoginItemSettings().openAtLogin }; }
  catch (e) { return { ok: false, error: String(e) }; }
});
ipcMain.handle('sunday:login-item-set', (_evt, enabled) => {
  try {
    app.setLoginItemSettings({ openAtLogin: !!enabled, openAsHidden: true });
    return { ok: true, openAtLogin: !!app.getLoginItemSettings().openAtLogin };
  } catch (e) { return { ok: false, error: String(e) }; }
});

app.on('activate', () => {
  if (!mainWindow) createMainWindow();
});
