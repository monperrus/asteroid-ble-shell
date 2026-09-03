# Asteroid BLE Shell

An opt-in Bluetooth Low Energy terminal for AsteroidOS on the Huawei Watch 2
(`sawfish`). It provides an interactive `ceres` shell when USB ADB and Wi-Fi
are unavailable.

This is a developer debugging facility, not a replacement for SSH and not a
general-purpose BLE serial service. The watch owner must explicitly enable it
locally before a previously trusted host can connect.

## Components

| Directory | Purpose |
| --- | --- |
| [`server/`](server/) | Modified `asteroid-btsyncd`: Nordic UART Service (NUS), access control, and a PTY running `/bin/sh -i` as `ceres`. |
| [`client/`](client/) | `ble-uart.py`, the Linux/Bleak terminal client. Its default is an SSH-like raw-terminal REPL. |
| [`app/`](app/) | On-watch QML tile used to arm or disarm the service locally. |

The server originated from [AsteroidOS/asteroid-btsyncd](https://github.com/AsteroidOS/asteroid-btsyncd).
Its original checkout metadata is retained only in the ignored local
`.upstream-metadata/` migration archive; it is not part of this repository.

## Architecture

```text
watch UI -- Arm(15 min) --> btsyncd/NUS -- encrypted BLE --> ble-uart.py --> terminal
                                  |
                                  +-- PTY: /bin/sh -i as ceres
```

NUS is a byte stream. The client subscribes to TX notifications before sending
RX bytes; the server passes accepted bytes only to a PTY, never to `sh -c`.
The PTY starts at 80×24. Baseline NUS has no terminal-resize control channel,
so full-screen programs do not follow local terminal resizes.

## Quick start

### Deploy the daemon

Build [`server/`](server/) against the exact AsteroidOS/Qt ABI on the watch,
then deploy it through the normal AsteroidOS package workflow. The service must
run as `ceres`, never root. Keep a known working package or binary for rollback
before replacing the system service binary.

A full Yocto/IPK build is the supported deployment path. Do not commit local
binaries, SDK sysroots, downloaded IPKs, or native build outputs here.

### Install the watch control tile

With ADB connected for setup:

```sh
./app/deploy-adb.sh
```

Open **BLE SHELL** in the launcher and tap the large centre dial until it says
**SHELL ARMED**. This enables a 15-minute access window.

### Connect from Linux

```sh
python3 -m pip install bleak
python3 client/ble-uart.py --adapter hci0 43:43:A1:12:1F:AC
```

The default is an interactive shell REPL. Enter, Backspace, Ctrl-C, Ctrl-D and
ANSI terminal output are forwarded to the watch PTY. Press `Ctrl-]` locally to
disconnect. Use a paired watch address or advertised name; select another host
controller with `--adapter` if needed.

For a pipe or automated smoke test, use line mode explicitly:

```sh
printf 'uname -a\r' | python3 client/ble-uart.py --line-mode 43:43:A1:12:1F:AC
```

## Security model

An arbitrary nearby BLE device cannot open the shell. A UART session requires:

1. Local arm by the watch owner. It starts disabled after boot and has a
   bounded deadline; the UI uses 15 minutes.
2. BlueZ encrypted, authenticated GATT access: RX is
   `encrypt-authenticated-write`; TX is `encrypt-authenticated-notify`.
3. A peer that is already both paired and trusted on the watch.
4. TX notification subscription before RX input is accepted.
5. A single peer identity pinned for the active session.

Disarm, expiry, disconnect, TX-notification stop, and idle timeout all destroy
the PTY process group. The daemon refuses to spawn a shell when running as root
or as a user other than `ceres`.

Treat every paired-and-trusted device as an administrator-capable terminal
while BLE UART is armed. Remove stale pairings and arm only when needed. The
pairing's MITM properties should be independently confirmed with an over-the-
air trace for each target image and controller.

## Operations and recovery

- NUS is registered at daemon startup, even while disabled; arming controls
  session access rather than GATT visibility.
- The app controls `org.asteroidos.btsyncd` on the `ceres` session bus, at
  `/org/asteroidos/btsyncd/uart`, interface `org.asteroidos.BleUart1`.
- The client deliberately avoids awaiting an explicit Bleak disconnect because
  of a BlueZ D-Bus race. Process exit releases the link and disarms the watch.
- If `bluetoothd` is stuck in uninterruptible kernel sleep, restarting its
  service cannot recover it. Reboot the watch over USB, wait for Bluetooth to
  power on, restart `asteroid-btsyncd`, then arm again.

## Pre-flight checks

Before relying on BLE alone, verify that the tile can arm/disarm; the host can
discover NUS; the client opens a `ceres` prompt; disconnect terminates a
long-running shell; and unpaired, untrusted, expired, or unsubscribed peers
cannot inject bytes.

## License

The daemon retains the upstream GPL-3.0-or-later license in
[`LICENSE`](LICENSE). Preserve upstream notices in server modifications.
