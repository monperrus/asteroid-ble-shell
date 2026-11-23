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
scripts. It sends LF-terminated lines as CR, like a terminal
Enter. Both modes exit with status 1 when the shell does not answer an initial
empty command line within `--arm-timeout` seconds: BLE UART is not armed. They also exit
with status 1, naming what was lost, when a write stalls for 10 s or input lines are
still unsent after `--drain-timeout` seconds.

Each write uses the smaller of the negotiated payload size and the 512-byte
GATT attribute limit. With a 517-byte ATT MTU, BlueZ can advertise a 514-byte
write payload. The watch silently discards Write Commands over 512 bytes.
Write Commands carry no acknowledgement, so for scripted output use an
end marker or a checksum to verify the result.

This is deliberately close to an SSH shell, with one transport limitation:
baseline NUS has no terminal-resize/control channel, so full-screen programs
start at the watch PTY's fixed 80×24 size and will not follow local window
resizes.

The client subscribes to TX before transmitting any input. It intentionally
does not call `BleakClient.disconnect()` at exit because this host's
Bleak/BlueZ D-Bus combination can report a spurious `EOFError` during that
race; closing the client process releases the link cleanly.

## File transfer: `watch-fs.py`

`watch-fs.py` reads and writes files over the [Adafruit BLE File Transfer
protocol](https://github.com/adafruit/Adafruit_CircuitPython_BLE_File_Transfer)
v4 (service `0xFEBB`), the protocol InfiniTime also uses. Remote `/` is
`/home/ceres`. The watch side of this service is not in `server/` yet.

```sh
python3 client/watch-fs.py 43:43:A1:12:1F:AC ls /
python3 client/watch-fs.py 43:43:A1:12:1F:AC put song.mp3 /Music/ --verify
python3 client/watch-fs.py 43:43:A1:12:1F:AC get /sleep/2026-11-23.csv
python3 client/watch-fs.py 43:43:A1:12:1F:AC --json ls /sleep
```

Commands: `ls`, `get` (`-` for stdout), `put` (a trailing `/` keeps the
basename; `--verify` reads the file back and compares SHA-256), `rm`
(recursive), `mkdir` (with parents), `mv`. Only `ls`, `get -` and `--json`
print anything on success; exit status 1 means the watch refused (the status
code is in the `--json` error) or the link failed.

`fts.py` is the transport-independent protocol client. `tests/fts_model.py` is
the reference server model the watch implementation follows, and
`tests/test_fts.py` checks it against Adafruit's own client
(`tests/vendor/`).
