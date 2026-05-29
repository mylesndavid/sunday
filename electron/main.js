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

const { app, BrowserWindow, Tray, Menu, MenuItem, ipcMain, shell, nativeImage, desktopCapturer, systemPreferences } = require('electron');
const { autoUpdater } = require('electron-updater');
const path = require('node:path');
const fs   = require('node:fs');
const os   = require('node:os');
const satellite = require('./satellite');

const PREFS_FILE = () => path.join(app.getPath('userData'), 'prefs.json');

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
  // Auth token: prefer the saved pref; if missing AND the daemon is local
  // (127.0.0.1), read the daemon's own token file. Lets the same code path
  // work for both "local daemon on this Mac" and "remote daemon I pasted in
  // during onboarding".
  let daemonToken = prefs.daemonToken || process.env.SUNDAY_DAEMON_TOKEN || '';
  const httpUrl = process.env.SUNDAY_DAEMON_HTTP || prefs.daemonHttp || 'http://127.0.0.1:8765';
  if (!daemonToken && /^http:\/\/(127\.0\.0\.1|localhost)/.test(httpUrl)) {
    try {
      const p = path.join(os.homedir(), '.sunday', 'auth.token');
      if (fs.existsSync(p)) daemonToken = fs.readFileSync(p, 'utf8').trim();
    } catch {}
  }
  return {
    daemonHttp: httpUrl,
    daemonWs:   process.env.SUNDAY_DAEMON_WS   || prefs.daemonWs   || 'ws://127.0.0.1:8765/v1/ws',
    daemonToken,
    // Onboarded once we have BOTH a usable token (from prefs, or the local
    // daemon's own file) AND the onboarding flag. The file-read covers a
    // fresh local install where the embedded daemon minted the token but the
    // user hasn't saved anything to prefs yet.
    onboarded:  !!daemonToken && !!prefs.onboarded,
  };
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

async function startEmbeddedDaemon() {
  if (daemonChild) return true;
  if (!isLocalDaemon()) return false;          // remote daemon — not ours to run
  if (await daemonHealthy()) return true;       // already running (dev `sunday start`)
  const bin = bundledDaemonBinary();
  if (!bin) { console.warn('no bundled daemon binary'); return false; }
  console.log('spawning embedded daemon:', bin);
  daemonChild = require('node:child_process').spawn(bin, [], {
    stdio: 'ignore',
    env: { ...process.env },
    detached: false,
  });
  daemonChild.on('exit', (code) => { console.warn('daemon exited', code); daemonChild = null; });
  daemonChild.on('error', (e) => { console.warn('daemon spawn error', e?.message); daemonChild = null; });
  // Wait up to ~20s for it to come up.
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await daemonHealthy()) { console.log('embedded daemon healthy'); return true; }
  }
  console.warn('embedded daemon never became healthy');
  return false;
}

function stopEmbeddedDaemon() {
  try { daemonChild?.kill('SIGTERM'); } catch {}
  daemonChild = null;
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
    backgroundColor: '#fbfaf7',
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
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
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
  return { daemonHttp, daemonWs, daemonToken };
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
    // Restart embedded daemon so it reloads credentials.
    if (daemonChild) { stopEmbeddedDaemon(); await new Promise((r) => setTimeout(r, 800)); }
    await startEmbeddedDaemon();
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
    backgroundColor: '#fbfaf7',
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
function startTrayStatus() {
  const tick = async () => {
    if (!tray || tray.isDestroyed?.()) return;
    try {
      const { daemonHttp } = resolveDaemon();
      const res = await fetch(`${daemonHttp}/v1/status`, { signal: AbortSignal.timeout(2500), headers: { ..._bearer() } });
      if (!res.ok) return;
      const d = await res.json();
      const n = Array.isArray(d.agents) ? d.agents.length : 0;
      tray.setTitle(n > 0 ? ` ${n}` : '');
      tray.setToolTip(n > 0 ? `Sunday — ${n} agent${n > 1 ? 's' : ''} working` : 'Sunday');
    } catch { /* daemon not up / offline — leave the title as-is */ }
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

function rebuildTrayMenu() {
  if (!tray) return;
  const { daemonHttp, onboarded } = resolveDaemon();
  const prefs = loadPrefs();
  const version = app.getVersion();
  const menu = Menu.buildFromTemplate([
    { label: `Sunday ${version}`, enabled: false },
    { label: `${prefs.label || daemonHttp}`, enabled: false },
    { type: 'separator' },
    { label: 'Chat',     accelerator: 'Command+1', click: () => switchToView('chat') },
    { label: 'Memory',   accelerator: 'Command+2', click: () => switchToView('memory') },
    { label: 'Settings…', accelerator: 'Command+,', click: () => switchToView('settings') },
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
    satellite.start(prefs);
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
  stopObserver();
  stopEmbeddedDaemon();
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
  running: !!(captureWindow && observerState.active),
  mic: micStatus(),
  error: observerState.error,
  lastChunkAt: observerState.lastChunkAt,
}));

ipcMain.handle('sunday:observer-set', async (_evt, on) => {
  if (on) { await startObserver(); savePrefs({ observer: true }); }
  else    { stopObserver();        savePrefs({ observer: false }); }
  return {
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
    const { daemonHttp } = resolveDaemon();
    const bin = app.isPackaged
      ? path.join(process.resourcesPath, 'NotchHUD.app', 'Contents', 'MacOS', 'notch-hud')
      : path.join(__dirname, 'build', 'NotchHUD.app', 'Contents', 'MacOS', 'notch-hud');
    notchHudChild = require('node:child_process').spawn(bin, [daemonHttp], { stdio: 'ignore' });
    notchHudChild.on('exit', () => { notchHudChild = null; });
  } catch { notchHudChild = null; }
}
function stopNotchHud() {
  try { notchHudChild?.kill(); } catch { /* already gone */ }
  notchHudChild = null;
}

app.on('activate', () => {
  if (!mainWindow) createMainWindow();
});
