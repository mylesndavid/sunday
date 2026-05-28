#!/usr/bin/env bash
# Sunday — one-line install for the desktop app.
#
# Downloads the latest signed-by-no-one DMG from GitHub Releases, mounts it,
# copies Sunday.app to /Applications, and strips the quarantine flag so macOS
# doesn't yell about an unidentified developer on first launch.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mylesndavid/sunday/main/install.sh | bash

set -euo pipefail

REPO="mylesndavid/sunday"
APP="Sunday.app"
DEST="/Applications/${APP}"

note()  { printf "\033[1;36m→\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
fail()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "Sunday's desktop app is macOS-only right now."

arch="$(uname -m)"
case "$arch" in
  arm64)   pattern='arm64\.dmg' ;;
  x86_64)  pattern='Sunday-[0-9.]+\.dmg' ;;  # Intel DMG has no arch suffix
  *)       fail "Unsupported architecture: $arch" ;;
esac

note "Finding the latest release on $REPO …"
url="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
       | grep -Eo "https://[^\"]+${pattern}" | head -n1 || true)"
[[ -n "$url" ]] || fail "No matching DMG found for $arch in the latest release."
ok "Found: $(basename "$url")"

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
dmg="$tmp/sunday.dmg"

note "Downloading …"
curl -fL --progress-bar "$url" -o "$dmg"

note "Mounting …"
mount_out="$(hdiutil attach -nobrowse -quiet "$dmg")"
mountpoint="$(echo "$mount_out" | tail -n1 | awk '{ $1=""; $2=""; sub(/^  */,""); print }')"
[[ -d "$mountpoint" ]] || fail "Could not locate mount point."

if [[ -d "$DEST" ]]; then
  note "Replacing existing ${DEST} …"
  rm -rf "$DEST"
fi

note "Copying ${APP} to /Applications …"
cp -R "$mountpoint/$APP" "$DEST"

hdiutil detach -quiet "$mountpoint" || true

# Unsigned build — strip Gatekeeper's quarantine attribute so it launches
# without the "Sunday is damaged / can't be opened" dialog. Notarization is
# the right long-term fix; until then this is the install-time workaround.
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

ver="$(defaults read "$DEST/Contents/Info.plist" CFBundleShortVersionString 2>/dev/null || echo "?")"
ok "Sunday $ver installed at $DEST"
note "Launch it from /Applications, or run: open -a Sunday"
