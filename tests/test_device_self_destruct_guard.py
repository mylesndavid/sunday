"""The brain must never be able to kill the daemon that's running the turn.

A texted "restart yourself" — or a pasted setup script — once made the iMessage
brain call device_run_command with `launchctl bootout com.sunday.daemon`, which
SIGTERM'd the daemon mid-reply: "sunday down" landed before the answer sent, so
texting (even "hey", once the history was poisoned) silently never replied.
_would_self_destruct refuses exactly those teardown commands while leaving
read-only inspection (`launchctl list | grep sunday`) alone.
"""

import pytest

from sunday.devices.tools import _would_self_destruct


@pytest.mark.parametrize("command", [
    "bash /Users/Shared/imessage-doctor.sh",
    "launchctl bootout gui/503/com.sunday.daemon",
    "launchctl bootout gui/$(id -u)/com.sunday.daemon && launchctl bootstrap gui/$(id -u) $PL",
    "sudo launchctl kickstart -k gui/503/com.sunday.daemon",
    "launchctl unload ~/Library/LaunchAgents/com.sunday.daemon.plist",
    "launchctl stop com.sunday.imessage",
    "pkill -f sunday-daemon",
    "killall sunday-daemon",
    "pkill -f sunday",
    # multi-line / pasted snippet
    "PL=$(launchctl print gui/$(id -u)/com.sunday.daemon)\nlaunchctl bootout gui/$(id -u)/com.sunday.daemon",
])
def test_refuses_self_destruct(command):
    assert _would_self_destruct(command) is True


@pytest.mark.parametrize("command", [
    "echo hey",
    "launchctl list | grep sunday",
    "launchctl print gui/503/com.sunday.daemon",   # read-only inspect, no teardown verb
    "ps aux | grep sunday-daemon",                 # inspect, not kill
    "tail -n 40 ~/.sunday/logs/daemon-launchd.log",
    "ls -la ~/.sunday",
    "sqlite3 ~/Library/Messages/chat.db 'select count(*) from message'",
    "git status",
])
def test_allows_safe_commands(command):
    assert _would_self_destruct(command) is False
