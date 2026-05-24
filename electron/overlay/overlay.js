// Sunday overlay — minimal ambient state pill.

const pill  = document.getElementById('pill');
const label = document.getElementById('label');

function set(state) {
  pill.dataset.state = state.listening ? 'listening' : (state.connection || 'idle');
  if (state.listening) label.textContent = 'listening…';
  else if (state.connection === 'online')   label.textContent = 'Sunday';
  else if (state.connection === 'offline')  label.textContent = 'offline';
  else if (state.connection === 'connecting') label.textContent = 'connecting…';
}

if (window.sunday) {
  window.sunday.onOverlayState((s) => set(s || {}));
}

set({ connection: 'connecting' });
