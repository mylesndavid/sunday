"""Remote device support — install Sunday on any computer, talk to it from the main one.

A satellite daemon (`sunday-satellite`) runs on the remote machine and
connects outbound to the main Sunday over WebSocket. The main daemon
keeps a `DeviceManager` registry; tools on the main side dispatch
commands to satellites by device_id.

Capabilities a satellite advertises:
  - 'shell'      run shell commands
  - 'screen'     take screenshots
  - 'cdp'        launch Chrome / Electron with a shadow profile and drive
                 it through Chrome DevTools Protocol
"""
