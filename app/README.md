# BLE UART on-watch control

This QML launcher tile is the local safety control for the BLE UART debug
shell. It exposes a fixed **Enable for 15 minutes** action, current state and
remaining time, plus an immediate disable action. Tap the large central dial:
it says **TAP TO ENABLE** while off and toggles to immediate disable once
armed. The bottom icon provides the same action. When the daemon reports an
active shell it changes to red and plays the normal notification feedback.

It calls `org.asteroidos.BleUart1` on the `ceres` session bus; it does not
talk to BLE directly and cannot bypass pairing/trust checks.

## Install and remove

With USB ADB connected:

```sh
./app/deploy-adb.sh
```

The tile installs entirely below `/home/ceres/.local`. To remove it:

```sh
adb shell 'su -s /bin/sh ceres -c "rm -f ~/.local/share/qml-apps/ble-uart.qml ~/.local/share/applications/ble-uart.desktop; XDG_RUNTIME_DIR=/run/user/1000 DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user restart asteroid-launcher.service"'
```
