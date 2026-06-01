#!/usr/bin/env python3
# PyInstaller entry point for the bundled Sunday runtime. The Electron app
# spawns the compiled binary of this on launch, so a friend gets the brain
# without ever installing Python.
#
# Two modes from one frozen binary (the whole `sunday` package is bundled, so
# both entry points are already inside it):
#   sunday-daemon              -> the brain (sunday.daemon.main), the default
#   sunday-daemon satellite ... -> the device satellite (sunday.devices.satellite.main)
#
# The satellite mode is why a packaged install can connect a device at all:
# a friend's Mac has no repo/venv, so the app points its satellite launcher at
# this same binary with the `satellite` subcommand instead of a dev venv script.
import sys


def _run() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "satellite":
        # Drop "satellite" so the satellite's own argparse sees only its flags
        # (--server / --device-id / --token).
        del sys.argv[1]
        from sunday.devices.satellite import main as satellite_main
        satellite_main()
        return 0
    # Bundled runs are never a TTY, so the banner is skipped automatically; logs
    # go to stdout/stderr which the Electron parent captures.
    from sunday.daemon import main
    return main()


if __name__ == "__main__":
    sys.exit(_run())
