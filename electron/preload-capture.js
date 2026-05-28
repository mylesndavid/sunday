// Preload for the hidden ambient-observer capture window. Exposes the bare
// minimum: report capture state, and hand finished audio chunks to main.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('capture', {
  // Tell main whether getUserMedia succeeded (active) or failed (error).
  reportState: (state) => ipcRenderer.send('sunday:observer-capture-state', state),
  // Send a finished ~30s chunk (Uint8Array) to main for transcription + tick.
  sendChunk: (bytes) => ipcRenderer.invoke('sunday:observer-chunk', bytes),
});
