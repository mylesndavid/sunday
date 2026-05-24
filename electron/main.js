// Sunday desktop — Electron main process.
//
// The main process is intentionally thin: it spawns one main window pointed
// at the daemon's HTTP+WS endpoint (default http://127.0.0.1:8765), an
// always-on-top overlay pill, and brokers a couple of small IPC calls.
// All the real work lives in the renderer + the daemon.

const { app, BrowserWindow, ipcMain, Menu, shell } = require('electron');
const path = require('node:path');

const DAEMON_HTTP = process.env.SUNDAY_DAEMON_HTTP || 'http://127.0.0.1:8765';
const DAEMON_WS   = process.env.SUNDAY_DAEMON_WS   || 'ws://127.0.0.1:8765/v1/ws';

let mainWindow = null;
let overlayWindow = null;

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

ipcMain.handle('sunday:config', () => ({
  daemonHttp: DAEMON_HTTP,
  daemonWs: DAEMON_WS,
}));

ipcMain.on('sunday:overlay-state', (_evt, state) => {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send('sunday:overlay-state', state);
  }
});

app.whenReady().then(() => {
  createMainWindow();
  createOverlayWindow();
  Menu.setApplicationMenu(null);
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (!mainWindow) createMainWindow();
});
