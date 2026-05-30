// Preload for the hidden meeting-capture window.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('meetingCapture', {
  report: (state) => ipcRenderer.send('sunday:meeting-capture-state', state),
  chunk: (track, bytes) => ipcRenderer.invoke('sunday:meeting-chunk', track, bytes),
  onStop: (handler) => ipcRenderer.on('sunday:meeting-capture-stop', () => handler()),
  stopped: () => ipcRenderer.send('sunday:meeting-capture-stopped'),
});
