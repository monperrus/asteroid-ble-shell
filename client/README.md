# BLE UART client

`ble-uart.py` is an SSH-like terminal client for the Huawei Watch 2 BLE UART
shell. By default it opens the watch's interactive `ceres` shell in raw-terminal
mode: commands, prompt editing, Ctrl-C, Ctrl-D, ANSI colour, and interactive
programs go directly to its PTY. It uses Nordic UART Service (NUS), but the
watch must first be armed locally and the Linux host must already be paired and
trusted on both sides.

```sh
python3 -m pip install bleak
python3 client/ble-uart.py 43:43:A1:12:1F:AC
```

Pass a watch advertising name instead of an address to scan for it, and use
`--adapter hci1` to select a non-default BlueZ adapter. The default REPL needs
a real terminal and puts it in raw mode; press **Ctrl-]** to disconnect locally.
`--line-mode` is intentionally the non-interactive option for pipes and simple
scripts.

This is deliberately close to an SSH shell, with one transport limitation:
baseline NUS has no terminal-resize/control channel, so full-screen programs
start at the watch PTY's fixed 80×24 size and will not follow local window
resizes.

The client subscribes to TX before transmitting any input. It intentionally
does not call `BleakClient.disconnect()` at exit because this host's
Bleak/BlueZ D-Bus combination can report a spurious `EOFError` during that
race; closing the client process releases the link cleanly.
