# Building Sunday.app

Three paths depending on what you've got.

## 1. Unsigned local DMG (no Apple Developer account needed)

For testing the build flow or distributing to people you trust who know
to right-click → Open the first time.

```bash
cd electron
npm install
npm run dist:mac:unsigned
```

Output: `electron/dist/Sunday-<version>-{arm64,x64}.dmg` + `.zip`.

Recipients get an "unidentified developer" warning on first launch.
Right-click → Open bypasses it.

## 2. Signed local DMG (Apple Developer ID, no notarization)

Lets people open the app normally without warnings but doesn't survive
Gatekeeper's strictest checks (`Sequoia` requires notarization for
internet-downloaded apps).

```bash
export CSC_NAME="Developer ID Application: Your Name (TEAMID)"
cd electron
npm run dist:mac
```

`electron-builder` auto-discovers identities from your Keychain. Override
explicitly with the env var above if you have multiple.

## 3. Signed + notarized DMG (full distribution)

What you ship to the public.

```bash
export CSC_NAME="Developer ID Application: Your Name (TEAMID)"

# Notarization credentials — generate an app-specific password at
# https://appleid.apple.com → Sign-In and Security → App-Specific Passwords
export APPLE_ID="you@example.com"
export APPLE_APP_SPECIFIC_PASSWORD="xxxx-xxxx-xxxx-xxxx"
export APPLE_TEAM_ID="ABCD123456"

# Tell electron-builder to notarize after signing
export NOTARIZE=true

cd electron
npm run dist:mac
```

`electron-builder` submits the signed .app bundle to Apple, polls until
notarization completes (~5 minutes), then staples the ticket to the DMG.
The output passes Gatekeeper out of the box.

To enable notarization, update `package.json`'s `build.mac.notarize`
from `false` to:

```json
"notarize": {
  "teamId": "${env.APPLE_TEAM_ID}"
}
```

(electron-builder reads `APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` from
env automatically.)

## What's in this directory

- `entitlements.mac.plist` — hardened-runtime + mic + network entitlements.
- `icon.png` / `icon.icns` — app icon (drop one in if you have one;
  electron-builder uses a default otherwise).

## Verifying a built DMG

```bash
spctl -a -v Sunday.app          # should say "accepted" if signed+notarized
codesign -dv --verbose=4 Sunday.app
```
