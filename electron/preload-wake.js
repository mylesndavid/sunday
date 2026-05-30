// Preload for the hidden "Hey Sunday" wake window. Exposes the bare minimum:
// report capture state, and hand finished short audio windows to main.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('wake', {
  // Tell main whether getUserMedia succeeded (active) or failed (error).
  reportState: (state) => ipcRenderer.send('sunday:wake-capture-state', state),
  // Send a finished ~2.5s window (Uint8Array) to main for transcription + match.
  sendChunk: (bytes) => ipcRenderer.invoke('sunday:wake-chunk', bytes),
});
