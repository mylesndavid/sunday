// Bridge a narrow API into the renderer. No nodeIntegration, no remote.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('sunday', {
  getConfig: () => ipcRenderer.invoke('sunday:config'),
  saveConnection: (cfg) => ipcRenderer.invoke('sunday:save-connection', cfg),
  finishOnboarding: (config) => ipcRenderer.invoke('sunday:finish-onboarding', config),
  openSettings: () => ipcRenderer.invoke('sunday:open-settings'),
  requestScreen: () => ipcRenderer.invoke('sunday:request-screen'),
  requestControl: () => ipcRenderer.invoke('sunday:request-control'),
  rewindImage: (p) => ipcRenderer.invoke('sunday:rewind-image', p),
  openExternal: (url) => ipcRenderer.invoke('sunday:open-external', url),
  notchMetrics: () => ipcRenderer.invoke('sunday:notch-metrics'),
  notchMode: (mode) => ipcRenderer.send('sunday:notch-mode', mode),
  checkFDA: () => ipcRenderer.invoke('sunday:check-fda'),
  openFDASettings: () => ipcRenderer.invoke('sunday:open-fda-settings'),
  setOverlayState: (state) => ipcRenderer.send('sunday:overlay-state', state),
  onOverlayState: (handler) => {
    ipcRenderer.on('sunday:overlay-state', (_evt, state) => handler(state));
  },
  onOpenAdmin: (handler) => {
    ipcRenderer.on('sunday:open-admin', () => handler());
  },
  onSwitchView: (handler) => {
    ipcRenderer.on('sunday:switch-view', (_evt, name) => handler(name));
  },
  // ambient observer (mic → "what user is doing" → notch HUD + atoms)
  observerStatus: () => ipcRenderer.invoke('sunday:observer-status'),
  observerSet: (on) => ipcRenderer.invoke('sunday:observer-set', !!on),
});
