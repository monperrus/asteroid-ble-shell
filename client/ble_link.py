"""Open a Bleak connection to the watch that coexists with other holders of the link.

The laptop may already hold the watch's LE link (watch-autolock keeps it up),
or nothing may. Both must work, and a tool must leave the link as it found
it:

- an address is resolved to BlueZ's device object directly, because a
  connected watch does not advertise and Bleak's address scan would never
  find it; a name still needs a scan, so it only works while unconnected;
- ``org.bluez.Error.InProgress`` (another client is connecting right now) is
  retried for a few seconds;
- on exit, a link that existed before is kept: Bleak would otherwise send
  ``Device1.Disconnect``, which drops the link for every client. A link this
  tool opened is disconnected.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDBusError

CONNECT_ATTEMPTS = 10
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


async def bluez_connected(path: str) -> bool:
    """Device1.Connected as BlueZ reports it; False when the device is unknown."""
    from dbus_fast import BusType, Message, MessageType
    from dbus_fast.aio import MessageBus

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    try:
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface="org.freedesktop.DBus.Properties",
                member="Get",
                signature="ss",
                body=["org.bluez.Device1", "Connected"],
            )
        )
        return reply.message_type == MessageType.METHOD_RETURN and bool(reply.body[0].value)
    finally:
        bus.disconnect()


async def connect(client: BleakClient, **kwargs: Any) -> None:
    for attempt in range(CONNECT_ATTEMPTS):
        try:
            await client.connect(**kwargs)
            return
        except BleakDBusError as error:
            if error.dbus_error != "org.bluez.Error.InProgress" or attempt == CONNECT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(RETRY_DELAY)


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
        self.preexisting = await bluez_connected(target.details["path"])
        self.client = BleakClient(target, **self.client_kwargs, **bluez_kwargs(self.adapter))
        await connect(self.client, **({"timeout": self.connect_timeout} if self.connect_timeout else {}))
        return self.client

    async def __aexit__(self, *exc: object) -> None:
        await self.release()

    async def release(self) -> None:
        client, self.client = self.client, None
        if client is None:
            return
        if self.preexisting:
            keep_link(client)
            return
        # This host's BlueZ can report a spurious EOFError while the link goes
        # down (see client/README.md); the link is down either way.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
