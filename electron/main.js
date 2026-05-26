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

const { app, BrowserWindow, Tray, Menu, MenuItem, ipcMain, shell, nativeImage } = require('electron');
const path = require('node:path');
const fs   = require('node:fs');

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
let settingsWindow = null;
let tray = null;

function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 980,
    height: 720,
    minWidth: 540,
    minHeight: 480,
    backgroundColor: '#0f0e0d',
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

function createOverlayWindow() {
  overlayWindow = new BrowserWindow({
    width: 220,
    height: 64,
    x: 24,
    y: 24,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    focusable: false,
    hasShadow: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  overlayWindow.setAlwaysOnTop(true, 'floating');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  overlayWindow.loadFile(path.join(__dirname, 'overlay', 'index.html'));
  overlayWindow.on('closed', () => { overlayWindow = null; });
}

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
  createSettingsWindow();
  return true;
});

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

ipcMain.on('sunday:overlay-state', (_evt, state) => {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send('sunday:overlay-state', state);
  }
});

function createSettingsWindow() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 860,
    height: 720,
    minWidth: 600,
    minHeight: 540,
    backgroundColor: '#0f0e0d',
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
  settingsWindow.loadFile(path.join(__dirname, 'renderer', 'settings.html'));
  settingsWindow.on('closed', () => { settingsWindow = null; });
}

function createOnboardingWindow() {
  onboardingWindow = new BrowserWindow({
    width: 720,
    height: 620,
    minWidth: 540,
    minHeight: 480,
    backgroundColor: '#0f0e0d',
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
    { label: 'Open chat',  click: () => {
        if (!mainWindow) createMainWindow();
        else { mainWindow.show(); mainWindow.focus(); }
    }},
    { label: 'Admin console',  click: () => {
        if (!mainWindow) createMainWindow();
        else { mainWindow.show(); mainWindow.focus(); }
        // mainWindow renderer handles ⌘. — send a message to open the panel
        setTimeout(() => mainWindow?.webContents.send('sunday:open-admin'), 200);
    }},
    { label: 'Settings…',       accelerator: 'Command+,', click: () => createSettingsWindow() },
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
    // Overlay pill is opt-in — most users don't want a persistent
    // always-on-top indicator. The tray icon conveys presence + state.
    // Toggle via the tray menu when we wire it (not in this commit).
  }
  createTray();
  Menu.setApplicationMenu(null);
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (!mainWindow) createMainWindow();
});
