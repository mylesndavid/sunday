// Load signing/notarization secrets from ~/.sunday-build/secrets.env into
// the environment electron-builder sees. Secrets never live in the repo.
//
// Required env (all loaded from the file):
//   APPLE_ID                       Apple ID with Developer Program
//   APPLE_APP_SPECIFIC_PASSWORD    https://appleid.apple.com app-specific password
//   APPLE_TEAM_ID                  10-char team id
//   CSC_NAME                       full "Developer ID Application: ..." string
//
// If any are missing, the build aborts loudly so you don't ship an unsigned
// build by accident.

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// Local dev loads from the file; CI (GitHub Actions) provides the same keys as
// env vars from repo secrets, so the file is optional when env is already set.
const file = path.join(os.homedir(), '.sunday-build', 'secrets.env');
if (fs.existsSync(file)) {
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const k = trimmed.slice(0, eq).trim();
    const v = trimmed.slice(eq + 1).trim();
    if (!process.env[k]) process.env[k] = v;
  }
} else if (!process.env.APPLE_ID) {
  console.error(`\n  No signing secrets at ${file} and APPLE_ID not in env.`);
  console.error('  Local: create ~/.sunday-build/secrets.env. CI: set repo secrets.\n');
  process.exit(1);
}

const required = ['APPLE_ID', 'APPLE_APP_SPECIFIC_PASSWORD', 'APPLE_TEAM_ID', 'CSC_NAME'];
const missing = required.filter((k) => !process.env[k]);
if (missing.length) {
  console.error(`\n  Missing required signing env: ${missing.join(', ')}`);
  process.exit(1);
}

// electron-builder reads CSC_NAME for the identity; the team id + apple id +
// app-specific password are picked up by notarytool via env. No further work.
console.log(`  signing as ${process.env.CSC_NAME}, team ${process.env.APPLE_TEAM_ID}`);
