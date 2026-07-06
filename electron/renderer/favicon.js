// Favicon/logo resolution — a JS port of Dayflow's FaviconService.
//
// Order, matching Dayflow: check hardcoded brand/app patterns against the raw
// appSites string FIRST (these map to bundled PNG assets under icons/favicons —
// this is what gives native Mac apps like Terminal/Finder/iMessage real logos),
// and only then fall back to a network favicon for a normalized host (Google S2,
// then the site's own /favicon.ico). Everything degrades to nothing on failure.

// pattern (substring, lowercased) → bundled asset file (icons/favicons/<name>.png).
// Order matters: first match wins, so more specific patterns go first.
const ASSET_PATTERNS = [
  ['dayflow', 'DayflowFavicon'],
  ['youtube', 'YouTubeFavicon'], ['youtu.be', 'YouTubeFavicon'],
  ['reddit', 'RedditFavicon'],
  ['twitter', 'XFavicon'], ['x.com', 'XFavicon'],
  ['leagueoflegends', 'LeagueOfLegendsFavicon'], ['league of legends', 'LeagueOfLegendsFavicon'],
  ['meet.google', 'GoogleFavicon'], ['google meet', 'GoogleFavicon'],
  ['imessage', 'iMessageFavicon'], ['messages', 'MessagesFavicon'],
  ['facetime', 'FaceTimeFavicon'], ['findmy', 'FindMyFavicon'], ['find my', 'FindMyFavicon'],
  ['icloud.com/mail', 'MailFavicon'], ['icloud.com/calendar', 'CalendarFavicon'],
  ['icloud.com/notes', 'NotesFavicon'], ['icloud.com/reminders', 'RemindersFavicon'],
  ['icloud.com/photos', 'PhotosFavicon'],
  ['music.apple', 'MusicFavicon'], ['tv.apple', 'TVFavicon'], ['news.apple', 'NewsFavicon'],
  ['books.apple', 'BooksFavicon'], ['podcasts.apple', 'PodcastsFavicon'], ['maps.apple', 'MapsFavicon'],
  ['weather.apple', 'WeatherFavicon'], ['fitness.apple', 'FitnessFavicon'], ['health.apple', 'HealthFavicon'],
  ['wallet.apple', 'WalletFavicon'], ['freeform.apple', 'FreeformFavicon'],
  ['shortcuts.apple', 'ShortcutsFavicon'], ['translate.apple', 'TranslateFavicon'],
  ['passwords.apple', 'PasswordsFavicon'], ['apps.apple', 'AppStoreFavicon'],
  ['keynote', 'KeynoteFavicon'], ['numbers', 'NumbersFavicon'], ['pages.apple', 'PagesFavicon'],
  ['safari', 'SafariFavicon'], ['finder', 'FinderFavicon'],
  ['system preferences', 'SettingsFavicon'], ['system settings', 'SettingsFavicon'], ['settings', 'SettingsFavicon'],
  ['calculator', 'CalculatorFavicon'], ['preview', 'PreviewFavicon'], ['contacts', 'ContactsFavicon'],
  ['voice memos', 'VoiceMemosFavicon'], ['voicememos', 'VoiceMemosFavicon'],
  ['app store', 'AppStoreFavicon'], ['appstore', 'AppStoreFavicon'],
  ['ghostty', 'GhosttyFavicon'], ['iterm', 'iTerm2Favicon'], ['terminal', 'TerminalFavicon'],
  ['xcode', 'XCodeFavicon'],
  ['visual studio code', 'VSCodeFavicon'], ['vs code', 'VSCodeFavicon'], ['vscode', 'VSCodeFavicon'],
  ['google chrome', 'ChromeFavicon'], ['chrome', 'ChromeFavicon'],
];

// Generic words that need "apple" context to avoid false positives (both must match).
const DUAL_PATTERNS = [
  ['mail', 'apple', 'MailFavicon'], ['calendar', 'apple', 'CalendarFavicon'],
  ['notes', 'apple', 'NotesFavicon'], ['reminders', 'apple', 'RemindersFavicon'],
  ['photos', 'apple', 'PhotosFavicon'], ['home', 'apple', 'HomeFavicon'],
  ['stocks', 'apple', 'StocksFavicon'], ['files', 'apple', 'FilesFavicon'],
  ['clock', 'apple', 'ClockFavicon'], ['music', 'apple', 'MusicFavicon'],
  ['tv', 'apple', 'TVFavicon'], ['news', 'apple', 'NewsFavicon'],
  ['books', 'apple', 'BooksFavicon'], ['podcasts', 'apple', 'PodcastsFavicon'],
  ['weather', 'apple', 'WeatherFavicon'], ['translate', 'apple', 'TranslateFavicon'],
];

// Brands Dayflow ships as vector logos (no PNG here) but which have great web
// favicons — map them to a canonical domain so S2 resolves the real logo.
const BRAND_DOMAIN = [
  ['chat.openai', 'chatgpt.com'], ['chatgpt', 'chatgpt.com'], ['openai', 'openai.com'],
  ['claude', 'claude.ai'], ['anthropic', 'anthropic.com'], ['gemini', 'gemini.google.com'],
  ['github', 'github.com'], ['discord', 'discord.com'], ['cursor', 'cursor.com'],
  ['slack', 'slack.com'], ['notion', 'notion.so'], ['figma', 'figma.com'],
  ['linear', 'linear.app'], ['spotify', 'spotify.com'], ['zoom', 'zoom.us'],
];

const HOST_ALIASES = { 'codex.com': 'chatgpt.com', 'codex.so': 'chatgpt.com' };

function normalizedHost(site) {
  if (!site) return null;
  let s = String(site).trim().toLowerCase();
  if (!s) return null;
  try {
    if (s.includes('://')) return new URL(s).host || null;
    if (s.includes('/')) return new URL('https://' + s).host || null;
  } catch { /* fall through */ }
  if (!s.includes('.')) return null;   // a bare app name has no host — patterns handle those
  return s;
}
const alias = (h) => (h && HOST_ALIASES[h]) || h;

// Pattern tables only → {asset} | {host} | null (no network).
function lookup(raw) {
  if (!raw) return null;
  const r = String(raw).toLowerCase();
  for (const [pat, asset] of ASSET_PATTERNS) if (r.includes(pat)) return { asset };
  for (const [p1, p2, asset] of DUAL_PATTERNS) if (r.includes(p1) && r.includes(p2)) return { asset };
  for (const [pat, dom] of BRAND_DOMAIN) if (r.includes(pat)) return { host: dom };
  return null;
}
const hostSpec = (site) => { const h = alias(normalizedHost(site)); return h ? { host: h } : null; };

// Resolve a card to an icon spec, mirroring Dayflow's primary→host→secondary→
// host→fallback order. Raw strings drive pattern matches; hosts drive network.
function resolve(ev) {
  const fb = ev.dominant_app || ev.title || '';
  return lookup(ev.app_primary || fb)
      || hostSpec(ev.app_primary)
      || lookup(ev.app_secondary)
      || hostSpec(ev.app_secondary)
      || lookup(fb);
}

// Build an <img> for a card, or null if nothing resolves. Bundled asset → no
// network. Host → Google S2, falling back to the site's own /favicon.ico.
export function cardIcon(ev, size = 15) {
  const spec = resolve(ev);
  if (!spec) return null;
  const img = document.createElement('img');
  img.className = 'tl-fav';
  img.width = size; img.height = size;
  img.loading = 'lazy';
  img.alt = '';
  if (spec.asset) {
    img.src = `icons/favicons/${spec.asset}.png`;
    img.addEventListener('error', () => img.remove());
    return img;
  }
  const host = spec.host;
  img.src = `https://www.google.com/s2/favicons?domain=${encodeURIComponent(host)}&sz=64`;
  let tried = 0;
  img.addEventListener('error', () => {
    if (tried === 0) { tried = 1; img.src = `https://${host}/favicon.ico`; }
    else img.remove();
  });
  return img;
}
