// Bridge a narrow API into the renderer. No nodeIntegration, no remote.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('sunday', {
  getConfig: () => ipcRenderer.invoke('sunday:config'),
  setOverlayState: (state) => ipcRenderer.send('sunday:overlay-state', state),
  onOverlayState: (handler) => {
    ipcRenderer.on('sunday:overlay-state', (_evt, state) => handler(state));
  },
});
