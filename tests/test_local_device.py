"""The brain's own host is always a reachable device.

device_run_command used to require a satellite holding a live WebSocket to the
brain. That link silently dropped on restarts, so the brain — running on the
very Mac it wanted to run a command on — would answer "no device, start the
satellite". register_local() installs an in-process device so shell works the
moment the daemon is up, no satellite needed.
"""

import pytest

from sunday.devices import local
from sunday.devices.manager import DeviceManager


@pytest.mark.asyncio
async def test_local_device_runs_command_without_websocket():
    mgr = DeviceManager()
    mgr.register_local(
        device_id="local",
        capabilities=local.CAPABILITIES,
        platform="test",
        handlers=local.HANDLERS,
    )

    # It shows up in the device list with the shell capability...
    devices = mgr.list_devices()
    assert any(d["device_id"] == "local" and "shell" in d["capabilities"] for d in devices)

    # ...and command() dispatches to the in-process handler (real subprocess),
    # never touching a WebSocket.
    result = await mgr.command("local", "run_command", {"command": "echo hello-local"})
    assert result["exit_code"] == 0
    assert "hello-local" in result["stdout"]


@pytest.mark.asyncio
async def test_local_device_resolves_for_shell_capability():
    # Mirrors how _t_device_run_command picks a target: auto-select a connected
    # device advertising 'shell' with no explicit id. The local device must be
    # eligible so the tool stops returning the "no connected device" error.
    from sunday.devices.tools import _resolve_device

    mgr = DeviceManager()
    mgr.register_local("local", local.CAPABILITIES, "test", local.HANDLERS)

    class _Ctx:
        # _devices_manager(ctx) reads ctx.extras["devices"]
        extras = {"devices": mgr}

    device_id, err = _resolve_device(_Ctx(), None, capability="shell")
    assert err is None
    assert device_id == "local"


@pytest.mark.asyncio
async def test_unknown_local_method_errors_cleanly():
    mgr = DeviceManager()
    mgr.register_local("local", local.CAPABILITIES, "test", local.HANDLERS)
    with pytest.raises(RuntimeError, match="no handler"):
        await mgr.command("local", "screenshot", {})
