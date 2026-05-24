// "Hey Sunday" wake-word listener.
//
// v0.1 uses the browser's continuous SpeechRecognition (free, works in
// Electron's Chromium, uses Google's recognizer over the network for
// most builds). When it hears the phrase, it fires onTrigger and stops
// — the main composer then arms a focused voice recording for the
// actual utterance.
//
// Future upgrades:
//   - swap to Porcupine Web (offline + privacy, requires a license key)
//   - swap to a local Whisper VAD loop
// Both are drop-in replacements behind initWakeWord() / stopWakeWord().

const WAKE_PHRASES = [
  /\bhey\s+sun\s*day\b/i,
  /\bhey\s+sunny\b/i,
  /\bok\s+sun\s*day\b/i,
];

let activeRecognition = null;
let active = false;

export function initWakeWord({ onTrigger }) {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    console.info('SpeechRecognition not available; wake word disabled.');
    return;
  }
  active = true;
  spin(Recognition, onTrigger);
}

export function stopWakeWord() {
  active = false;
  if (activeRecognition) {
    try { activeRecognition.stop(); } catch {}
    activeRecognition = null;
  }
}

function spin(Recognition, onTrigger) {
  if (!active) return;
  activeRecognition = new Recognition();
  activeRecognition.lang = 'en-US';
  activeRecognition.interimResults = true;
  activeRecognition.continuous = true;
  activeRecognition.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const text = (e.results[i][0].transcript || '').trim();
      if (!text) continue;
      if (WAKE_PHRASES.some((re) => re.test(text))) {
        try { activeRecognition.stop(); } catch {}
        if (typeof onTrigger === 'function') onTrigger(text);
        // Restart after a beat so we can hear the next "Hey Sunday".
        setTimeout(() => spin(Recognition, onTrigger), 1500);
        return;
      }
    }
  };
  activeRecognition.onerror = () => {
    // Recognizer drops out periodically — back off and respin.
    setTimeout(() => spin(Recognition, onTrigger), 1000);
  };
  activeRecognition.onend = () => {
    // Some Chromium builds end the continuous stream every few minutes.
    if (active) setTimeout(() => spin(Recognition, onTrigger), 500);
  };
  try {
    activeRecognition.start();
  } catch (err) {
    console.warn('wake word failed to start, retrying', err);
    setTimeout(() => spin(Recognition, onTrigger), 2000);
  }
}
