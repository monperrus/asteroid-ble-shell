"""Open a Bleak connection to the watch that coexists with other holders of the link.

The laptop may already hold the watch's LE link (watch-autolock keeps it up),
or nothing may. Both must work, and a tool must leave the link as it found
it:

- an address is resolved to BlueZ's device object directly, because a
  connected watch does not advertise and Bleak's address scan would never
  find it; a name still needs a scan, so it only works while unconnected;
- a link that is down is brought up with a plain ``Device1.Connect``, not
  through Bleak: when another client (watch-autolock reconnecting) is
  connecting right now, BlueZ answers ``org.bluez.Error.InProgress``, and
  Bleak's error path would then send ``Device1.Disconnect``, which cancels
  that other client's connect (with a retry each second, the link stayed down
  for 37 s). ``InProgress`` is retried for as long as such a connect may take
  (watch-autolock's runs up to 30 s), and the resulting link counts as the
  other client's;
- on exit, a link that existed before, or that another client was opening, is
  kept: Bleak would otherwise send ``Device1.Disconnect``, which drops the
  link for every client. So is a link a live program has registered as
  holding (see ``link_holders``): watch-autolock may lose the race to connect
  and still want the link. Any other link this tool opened is disconnected.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError

OTHER_CONNECT_WAIT = 35.0  # seconds to wait out another client's Connect (busctl --timeout=30)
RETRY_DELAY = 1.0
DISCONNECT_TIMEOUT = 10.0


def bluez_kwargs(adapter: str | None) -> dict:
    return {"bluez": {"adapter": adapter}} if adapter else {}


def device_path(address: str, adapter: str | None) -> str:
    return f"/org/bluez/{adapter or 'hci0'}/dev_{address.upper().replace(':', '_')}"


async def resolve(watch: str, adapter: str | None, scan_timeout: float) -> BLEDevice:
    """An address needs no scan; a name is looked up among advertisements."""
    if ":" in watch:
        return BLEDevice(watch.upper(), None, {"path": device_path(watch, adapter), "props": None})
    device = await BleakScanner.find_device_by_filter(
        lambda dev, adv: watch.casefold() in (dev.name or adv.local_name or "").casefold(),
        timeout=scan_timeout,
        **bluez_kwargs(adapter),
    )
    if device is None:
        raise RuntimeError(
            f"watch named {watch!r} was not found during scan "
            "(a connected watch does not advertise: pass its address)"
        )
    return device


async def _bluez_call(path: str, interface: str, member: str, signature: str = "",
                      body: list[Any] | None = None, timeout: float | None = None):
    from dbus_fast import BusType, Message
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        message = Message(destination="org.bluez", path=path, interface=interface,
                          member=member, signature=signature, body=body or [])
        return await asyncio.wait_for(bus.call(message), timeout)
    finally:
        bus.disconnect()


async def bluez_connected(path: str) -> bool:
    """Device1.Connected as BlueZ reports it; False when the device is unknown."""
    from dbus_fast import MessageType

    reply = await _bluez_call(path, "org.freedesktop.DBus.Properties", "Get", "ss",
                              ["org.bluez.Device1", "Connected"])
    return reply.message_type == MessageType.METHOD_RETURN and bool(reply.body[0].value)


async def bluez_connect(path: str, timeout: float) -> str | None:
    """Device1.Connect; None on success, else the D-Bus error name. Never disconnects."""
    from dbus_fast import MessageType

    reply = await _bluez_call(path, "org.bluez.Device1", "Connect", timeout=timeout)
    return None if reply.message_type == MessageType.METHOD_RETURN else reply.error_name


async def bring_up(path: str, timeout: float) -> bool:
    """Make sure the link is up; True when another client was connecting it meanwhile."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OTHER_CONNECT_WAIT
    other = False
    while not await bluez_connected(path):
        error = await bluez_connect(path, timeout)
        if error is None:
            break
        if error != "org.bluez.Error.InProgress" or loop.time() >= deadline:
            raise BleakDBusError(error, [])
        other = True
        await asyncio.sleep(RETRY_DELAY)
    return other


async def connect(client: BleakClient, **kwargs: Any) -> bool:
    """Connect; True when another client was connecting the device meanwhile."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + OTHER_CONNECT_WAIT
    other = False
    while True:
        try:
            await client.connect(**kwargs)
            return other
        except BleakDBusError as error:
            if error.dbus_error != "org.bluez.Error.InProgress" or loop.time() >= deadline:
                raise
            other = True
            await asyncio.sleep(RETRY_DELAY)


def holders_dir(watch: str) -> Path:
    """Where programs that keep the watch's link up register, one file per pid.

    A holder (watch-autolock) creates ``$XDG_RUNTIME_DIR/ble-link-holders/<ADDRESS>/<pid>``
    while it runs. Stale files of dead processes are ignored.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "ble-link-holders" / watch.upper()


def link_holders(watch: str) -> list[int]:
    """Pids of live programs registered as holding the watch's link."""
    alive = []
    with contextlib.suppress(OSError):
        for entry in holders_dir(watch).iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                os.kill(int(entry.name), 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                pass
            alive.append(int(entry.name))
    return sorted(alive)


def keep_link(client: BleakClient) -> None:
    """Stop Bleak from disconnecting the device when the event loop shuts down.

    Bleak's BlueZ backend (checked on 3.0.2) runs a disconnect monitor task
    that sends Device1.Disconnect when asyncio.run() cancels it. Setting its
    event ends the task normally instead. Private API: if it moves, the cost
    is the old behaviour (a dropped link), not a failure.
    """
    backend = getattr(client, "_backend", None)
    event = getattr(backend, "_disconnect_monitor_event", None)
    if event is not None:
        event.set()


class WatchLink:
    """``async with WatchLink(watch, adapter) as client:`` -- a connected BleakClient.

    Extra keyword arguments go to BleakClient (e.g. ``disconnected_callback``).
    """

    def __init__(self, watch: str, adapter: str | None = None, *, scan_timeout: float = 15,
                 connect_timeout: float | None = None, **client_kwargs: Any) -> None:
        self.watch = watch
        self.adapter = adapter
        self.scan_timeout = scan_timeout
        self.connect_timeout = connect_timeout
        self.client_kwargs = client_kwargs
        self.client: BleakClient | None = None
        self.preexisting = False

    async def __aenter__(self) -> BleakClient:
        target = await resolve(self.watch, self.adapter, self.scan_timeout)
        path = target.details["path"]
        self.preexisting = await bluez_connected(path)
        if not self.preexisting:
            self.preexisting = await bring_up(path, self.connect_timeout or 30)
        # The link is up now, so Bleak attaches to it without calling Connect.
        self.client = BleakClient(target, **self.client_kwargs, **bluez_kwargs(self.adapter))
        other = await connect(self.client, **({"timeout": self.connect_timeout} if self.connect_timeout else {}))
        self.preexisting = self.preexisting or other
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    async def release(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        if self.preexisting or (":" in self.watch and link_holders(self.watch)):
            keep_link(client)
            return
        # This host's BlueZ can report a spurious EOFError while the link goes
        # down (see client/README.md); the link is down either way.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
