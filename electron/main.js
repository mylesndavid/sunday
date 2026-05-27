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
const path = require('node:path');
const fs   = require('node:fs');
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
  return {
    daemonHttp: process.env.SUNDAY_DAEMON_HTTP || prefs.daemonHttp || 'http://127.0.0.1:8765',
    daemonWs:   process.env.SUNDAY_DAEMON_WS   || prefs.daemonWs   || 'ws://127.0.0.1:8765/v1/ws',
    onboarded:  !!prefs.onboarded,
  };
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
const NOTCH = {
  compact: { w: 260 },
  expanded: { w: 360, h: 320 },
};

function notchMetrics() {
  const { screen } = require('electron');
  const d = screen.getPrimaryDisplay();
  const topInset = Math.max(d.workArea.y - d.bounds.y, 0);
  const notchHeight = topInset > 0 ? topInset : 32;
  return { display: d, notchHeight };
}

function positionNotch(mode) {
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  const { display, notchHeight } = notchMetrics();
  const w = (mode === 'expanded' ? NOTCH.expanded.w : NOTCH.compact.w);
  const h = (mode === 'expanded' ? NOTCH.expanded.h : notchHeight + 10);
  const x = Math.round(display.bounds.x + display.bounds.width / 2 - w / 2);
  const y = display.bounds.y;   // absolute top — over the notch
  overlayWindow.setBounds({ x, y, width: w, height: h });
}

function createOverlayWindow() {
  const { notchHeight } = notchMetrics();
  overlayWindow = new BrowserWindow({
    width: NOTCH.compact.w,
    height: notchHeight + 10,
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
  positionNotch('compact');
}

ipcMain.handle('sunday:notch-metrics', () => ({ notchHeight: notchMetrics().notchHeight }));
ipcMain.on('sunday:notch-resize', (_evt, mode) => positionNotch(mode === 'expanded' ? 'expanded' : 'compact'));

ipcMain.handle('sunday:config', () => {
  const { daemonHttp, daemonWs } = resolveDaemon();
  return { daemonHttp, daemonWs };
});

ipcMain.handle('sunday:finish-onboarding', (_evt, { daemonHttp, daemonWs, label }) => {
  savePrefs({ daemonHttp, daemonWs, label, onboarded: true });
  if (!mainWindow) createMainWindow();
  if (onboardingWindow && !onboardingWindow.isDestroyed()) onboardingWindow.close();
  rebuildTrayMenu();
  return true;
});

ipcMain.handle('sunday:save-connection', (_evt, { daemonHttp, daemonWs }) => {
  savePrefs({ daemonHttp, daemonWs });
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

// Prompt for / open the macOS Accessibility grant so Sunday can control
// other apps. isTrustedAccessibilityClient(true) adds Sunday to the list
// and prompts; the satellite (a child of Sunday) inherits the grant.
ipcMain.handle('sunday:request-control', async () => {
  try {
    let trusted = false;
    if (systemPreferences.isTrustedAccessibilityClient) {
      trusted = systemPreferences.isTrustedAccessibilityClient(true);
    }
    await shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility');
    return { status: trusted ? 'granted' : 'prompted' };
  } catch (err) {
    return { status: 'error', error: String(err) };
  }
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
  // Tiny template image — macOS tints automatically to match the menu bar
  // theme. 18×18 is the canonical Apple template size; we draw a single
  // amber-ish dot (becomes black/white at runtime under template tint).
  const icon = nativeImage.createFromDataURL(
    'data:image/png;base64,' +
    'iVBORw0KGgoAAAANSUhEUgAAABIAAAASCAYAAABWzo5XAAAAhUlEQVQ4y2NkYGD4z0AB' +
    'YGJgYGBgZGRk+I+L8/8DEzkamRiZGBmYmBlYWZkZmJgYGRgZGZgYWBgYmRgZmBgYGRgY' +
    'mBgYGRgYGBgYGBkYmRgYGBkYmRgZGRgYGRiZGBgYGRgZGBgYGRgYGBgYGRgZGBkYGRgY' +
    'GRgZGBgYGRgZGBgYGRgYGBgYGAEACS8EBaBPRMAAAAAASUVORK5CYII='
  );
  icon.setTemplateImage(true);
  tray = new Tray(icon);
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

function rebuildTrayMenu() {
  if (!tray) return;
  const { daemonHttp, onboarded } = resolveDaemon();
  const prefs = loadPrefs();
  const menu = Menu.buildFromTemplate([
    { label: `Sunday — ${prefs.label || daemonHttp}`, enabled: false },
    { type: 'separator' },
    { label: 'Chat',     accelerator: 'Command+1', click: () => switchToView('chat') },
    { label: 'Memory',   accelerator: 'Command+2', click: () => switchToView('memory') },
    { label: 'Settings…', accelerator: 'Command+,', click: () => switchToView('settings') },
    { type: 'separator' },
    { label: (overlayWindow && !overlayWindow.isDestroyed()) ? 'Hide notch HUD' : 'Show notch HUD', click: () => {
        if (overlayWindow && !overlayWindow.isDestroyed()) { overlayWindow.close(); savePrefs({ hud: false }); }
        else { createOverlayWindow(); savePrefs({ hud: true }); }
        rebuildTrayMenu();
    }},
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
  const { onboarded } = resolveDaemon();
  if (!onboarded) {
    createOnboardingWindow();
  } else {
    createMainWindow();
    // The notch HUD is on by default — ambient agent count + status that
    // extends the notch. Toggle from the tray. Opt out with prefs.hud=false.
    if (loadPrefs().hud !== false) createOverlayWindow();
  }
  createTray();
  Menu.setApplicationMenu(null);

  // Own the satellite as a child process so its macOS TCC grants
  // (Screen Recording etc.) attribute to "Sunday", not a standalone
  // Python LaunchAgent. Only when onboarded + not explicitly disabled.
  const prefs = loadPrefs();
  if (prefs.onboarded && prefs.embeddedSatellite !== false) {
    satellite.start(prefs);
  }
});

// Trigger the macOS Screen Recording permission prompt for Sunday.app.
// desktopCapturer.getSources with a screen type forces the system to
// register Sunday in System Settings → Screen Recording. Once granted,
// the satellite child (responsible process = Sunday) can screencapture.
ipcMain.handle('sunday:request-screen', async () => {
  try {
    if (systemPreferences.getMediaAccessStatus) {
      const status = systemPreferences.getMediaAccessStatus('screen');
      if (status === 'granted') return { status: 'granted' };
    }
    await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: { width: 1, height: 1 } });
    // Open the pane so the user can flip the toggle if the prompt was
    // dismissed or already-decided.
    shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
    return { status: 'prompted' };
  } catch (err) {
    return { status: 'error', error: String(err) };
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  satellite.stop();
});

app.on('activate', () => {
  if (!mainWindow) createMainWindow();
});
