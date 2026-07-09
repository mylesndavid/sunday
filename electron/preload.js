// Bridge a narrow API into the renderer. No nodeIntegration, no remote.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('sunday', {
  getConfig: () => ipcRenderer.invoke('sunday:config'),
  daemonHealth: () => ipcRenderer.invoke('sunday:daemon-health'),
  readLogs: () => ipcRenderer.invoke('sunday:read-logs'),
  revealLogs: () => ipcRenderer.invoke('sunday:reveal-logs'),
  debugPacket: () => ipcRenderer.invoke('sunday:debug-packet'),
  resetApp: () => ipcRenderer.invoke('sunday:reset'),
  // Funnel renderer errors into the same shareable ~/.sunday/logs/app.log.
  logError: (msg) => { try { ipcRenderer.send('sunday:renderer-log', String(msg)); } catch { /* never throw */ } },
  saveConnection: (cfg) => ipcRenderer.invoke('sunday:save-connection', cfg),
  runMode: () => ipcRenderer.invoke('sunday:run-mode'),
  setRunMode: (mode) => ipcRenderer.invoke('sunday:set-run-mode', mode),
  migrateToLocal: () => ipcRenderer.invoke('sunday:migrate-to-local'),
  finishOnboarding: (config) => ipcRenderer.invoke('sunday:finish-onboarding', config),
  setOpenRouterKey: (key) => ipcRenderer.invoke('sunday:set-openrouter-key', key),
  localToken: () => ipcRenderer.invoke('sunday:local-token'),
  openSettings: () => ipcRenderer.invoke('sunday:open-settings'),
  requestScreen: () => ipcRenderer.invoke('sunday:request-screen'),
  requestControl: () => ipcRenderer.invoke('sunday:request-control'),
  requestMicrophone: () => ipcRenderer.invoke('sunday:request-microphone'),
  requestFullDisk: () => ipcRenderer.invoke('sunday:request-fulldisk'),
  permissionsStatus: () => ipcRenderer.invoke('sunday:permissions-status'),
  updateState:   () => ipcRenderer.invoke('sunday:update-state'),
  updateCheck:   () => ipcRenderer.invoke('sunday:update-check'),
  updateRestart: () => ipcRenderer.invoke('sunday:update-restart'),
  onUpdateState: (h) => ipcRenderer.on('sunday:update-state', (_evt, s) => h(s)),
  rewindImage: (p) => ipcRenderer.invoke('sunday:rewind-image', p),
  timelineVideo: (p) => ipcRenderer.invoke('sunday:timeline-video', p),
  openExternal: (url) => ipcRenderer.invoke('sunday:open-external', url),
  revealExtension: () => ipcRenderer.invoke('sunday:reveal-extension'),
  openChromeExtensions: () => ipcRenderer.invoke('sunday:open-chrome-extensions'),
  notchMetrics: () => ipcRenderer.invoke('sunday:notch-metrics'),
  notchMode: (mode) => ipcRenderer.send('sunday:notch-mode', mode),
  checkFDA: () => ipcRenderer.invoke('sunday:check-fda'),
  openFDASettings: () => ipcRenderer.invoke('sunday:open-fda-settings'),
  argusStatus: () => ipcRenderer.invoke('sunday:argus-status'),
  setArgus: (enabled) => ipcRenderer.invoke('sunday:argus-set', enabled),
  openArgus: () => ipcRenderer.invoke('sunday:argus-open'),
  loginItemGet: () => ipcRenderer.invoke('sunday:login-item-get'),
  loginItemSet: (enabled) => ipcRenderer.invoke('sunday:login-item-set', enabled),
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
  transcriptionStatus: () => ipcRenderer.invoke('sunday:transcription-status'),
  installLocalTranscription: () => ipcRenderer.invoke('sunday:install-local-transcription'),
  meetingBegin: () => ipcRenderer.invoke('sunday:meeting-begin'),
  meetingChunk: (track, bytes) => ipcRenderer.invoke('sunday:meeting-chunk', track, bytes),
  meetingFinalize: () => ipcRenderer.invoke('sunday:meeting-finalize-now'),
  meetingState: () => ipcRenderer.invoke('sunday:meeting-state'),
  meetingAudio: (cid) => ipcRenderer.invoke('sunday:meeting-audio', cid),
  onMeetingStopNow: (h) => ipcRenderer.on('sunday:meeting-stop-now', () => h()),
  onInstallLog: (handler) => ipcRenderer.on('sunday:install-log', (_evt, line) => handler(line)),
});
