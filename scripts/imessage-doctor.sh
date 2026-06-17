#!/bin/bash
# Sunday native iMessage — one-command setup + health check.
#
# Run IN the dedicated Sunday macOS account (no sudo needed — it only touches
# that account's own LaunchAgent, creds, and daemon):
#     bash imessage-doctor.sh
#
# It makes the native iMessage channel permanent (survives reboot) and verifies
# every link in the chain, printing a clear pass/fail you can act on. Re-run it
# anytime — it's idempotent. This is the hand-cranked precursor to an in-app
# "Connect iMessage" flow.
set -uo pipefail

U=$(id -u)
SUN="$HOME/.sunday"
LOG="$SUN/logs/daemon-launchd.log"
fails=0
ok(){   printf '  \033[32m[ok]\033[0m   %s\n' "$*"; }
bad(){  printf '  \033[31m[FAIL]\033[0m %s\n' "$*"; fails=$((fails+1)); }
warn(){ printf '  \033[33m[warn]\033[0m %s\n' "$*"; }

echo "== Sunday native iMessage doctor =="

# 1) locate the daemon LaunchAgent plist
PL=$(ls "$HOME/Library/LaunchAgents/"com.sunday*.plist 2>/dev/null | head -1)
if [ -n "$PL" ]; then ok "daemon plist: $PL"; else
  bad "no com.sunday*.plist in ~/Library/LaunchAgents (open Sunday.app once to install it)"; fi

# 2) bake the native flag into the plist (persists across reboot) + this session
if [ -n "$PL" ]; then
  /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables dict" "$PL" 2>/dev/null
  if /usr/libexec/PlistBuddy -c "Add :EnvironmentVariables:SUNDAY_IMESSAGE_NATIVE string 1" "$PL" 2>/dev/null \
     || /usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:SUNDAY_IMESSAGE_NATIVE 1" "$PL"; then
    ok "SUNDAY_IMESSAGE_NATIVE=1 written to plist (survives reboot)"
  else bad "could not write the flag into $PL"; fi
fi
launchctl setenv SUNDAY_IMESSAGE_NATIVE 1 2>/dev/null

# 3) credentials
if grep -Eq '^OPENROUTER_API_KEY=.+' "$SUN/credentials.env" 2>/dev/null; then
  ok "OpenRouter API key present"; else
  bad "OPENROUTER_API_KEY missing in $SUN/credentials.env (the brain can't reply without it)"; fi
if grep -q '^SENDBLUE' "$SUN/credentials.env" 2>/dev/null; then
  warn "Sendblue creds present here — remove them so this account doesn't double-reply"; else
  ok "no Sendblue creds (won't double-reply with the main account)"; fi

# 4) (re)start the daemon from the plist
if [ -n "$PL" ]; then
  launchctl bootout "gui/$U/com.sunday.daemon" 2>/dev/null
  sleep 1
  if launchctl bootstrap "gui/$U" "$PL" 2>/dev/null; then ok "daemon (re)started"; else
    bad "daemon bootstrap failed — try: launchctl bootstrap gui/$U \"$PL\""; fi
fi
echo "  ...waiting for the daemon to come up"; sleep 6

# 5) watcher + Full Disk Access (reads chat.db)
last=$(grep -aE "imessage watcher started|imessage watcher disabled" "$LOG" 2>/dev/null | tail -1)
case "$last" in
  *started*)  ok "iMessage watcher running (chat.db readable)";;
  *disabled*) bad "watcher can't read chat.db -> grant Full Disk Access to Sunday (System Settings > Privacy & Security)";;
  *)          bad "watcher never started — see $LOG";;
esac

# 6) Automation -> Messages (can it send?)
if [ "$(tail -n 80 "$LOG" 2>/dev/null | grep -ac 'Not authorized to send Apple events')" -gt 0 ]; then
  warn "a recent reply was blocked sending -> grant System Settings > Privacy & Security > Automation > Sunday > Messages"
else
  ok "no recent send-permission errors"
fi

echo
if [ "$fails" -eq 0 ]; then
  echo "All core checks passed. Text Sunday's iMessage to confirm a live reply."
else
  echo "$fails check(s) need attention above — fix, then re-run: bash imessage-doctor.sh"
fi
