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
`/home/ceres`. The watch serves it from `server/src/filetransferservice.cpp`
to any paired and trusted host, without arming.

```sh
python3 client/watch-fs.py 43:43:A1:12:1F:AC ls /
python3 client/watch-fs.py 43:43:A1:12:1F:AC put song.mp3 /Music/ --verify
python3 client/watch-fs.py 43:43:A1:12:1F:AC get /sleep/2026-11-23.csv
python3 client/watch-fs.py 43:43:A1:12:1F:AC --json ls /sleep
```

Pass the watch's address, not its name, while another program keeps the watch
connected (e.g. `watch-autolock`): a connected watch does not advertise, and
`watch-fs` then uses BlueZ's device object directly. It never disconnects the
watch on exit, so such a link survives.

Measured on the Huawei Watch 2 (512-byte writes, 2026-11-23, single runs): `put`
of 1 MiB took 38-63 s, `get` 41 s with the default 64 KiB read requests.
`tests/live_adafruit_interop.py ADDRESS` checks Adafruit's own client against a
live watch.

Commands: `ls`, `get` (`-` for stdout), `put` (a trailing `/` keeps the
basename; `--verify` reads the file back and compares SHA-256), `rm`
(recursive), `mkdir` (with parents), `mv`. Only `ls`, `get -` and `--json`
print anything on success; exit status 1 means the watch refused (the status
code is in the `--json` error) or the link failed.

`fts.py` is the transport-independent protocol client. `tests/fts_model.py` is
the reference server model; the watch's C++ engine (`server/src/fts_engine.cpp`)
ports it. `tests/test_fts.py` runs every protocol test against both, the C++
one built for the host with ASan/UBSan (`tests/fts_engine_host.cpp`), using
our client and Adafruit's own (`tests/vendor/`).
