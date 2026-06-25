// Notarize hook for electron-builder. Submits the signed .app to Apple,
// blocks until the notarization ticket is back, and staples it. Without
// stapling the user gets a Gatekeeper prompt on first launch; with it
// they don't.
//
// Run after signing (electron-builder calls this automatically via
// the `afterSign` config key).

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { notarize } = require('@electron/notarize');

// Load secrets here too — `npm run` chains via `&&` run as separate processes,
// so env vars set by ensure-secrets.js die before electron-builder spawns
// this afterSign hook.
(function loadSecrets() {
  const file = path.join(os.homedir(), '.sunday-build', 'secrets.env');
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const t = line.trim();
    if (!t || t.startsWith('#')) continue;
    const eq = t.indexOf('=');
    if (eq < 0) continue;
    const k = t.slice(0, eq).trim();
    const v = t.slice(eq + 1).trim();
    if (!process.env[k]) process.env[k] = v;
  }
})();

exports.default = async function notarizing(context) {
  const { electronPlatformName, appOutDir } = context;
  if (electronPlatformName !== 'darwin') return;

  const config = context.packager.config || {};
  const macConfig = config.mac || {};
  const identity = macConfig.identity;
  const signingDisabled = process.env.CSC_IDENTITY_AUTO_DISCOVERY === 'false' || identity === null || identity === 'null';
  if (signingDisabled) {
    console.log('  skipping notarization because macOS signing is disabled.');
    return;
  }

  const required = ['APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID'];
  const missing = required.filter((key) => !process.env[key]);
  if (missing.length) {
    throw new Error(`Cannot notarize: missing ${missing.join(', ')}`);
  }

  const appName = context.packager.appInfo.productFilename;
  const appPath = `${appOutDir}/${appName}.app`;

  console.log(`  notarizing ${appPath} (this can take a couple of minutes)…`);

  await notarize({
    tool: 'notarytool',
    appPath,
    appleId: process.env.APPLE_ID,
    appleIdPassword: process.env.APPLE_APP_SPECIFIC_PASSWORD,
    teamId: process.env.APPLE_TEAM_ID,
  });

  console.log('  notarization complete + stapled.');
};
