#!/usr/bin/env python3
"""Live check: Adafruit's own FileTransferClient (vendored, unmodified) against a watch.

Only its CircuitPython packet buffer is replaced, by Bleak notifications and
Write Commands, one notification per readinto() as on CircuitPython. Exits 0
and prints one JSON line when write/read/list/move/delete round-trip.

    python3 tests/live_adafruit_interop.py 43:43:A1:12:1F:AC
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import queue
import sys
import threading
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "client"))
import fts

_spec = importlib.util.spec_from_file_location("watch_fs", HERE.parent / "client" / "watch-fs.py")
watch_fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch_fs)
# The vendored Adafruit client, loaded with BLE stand-ins.
from test_fts import adafruit


class BleakPacketBuffer:
    def __init__(self, loop: asyncio.AbstractEventLoop, ble, char, max_write: int) -> None:
        self.loop, self.ble, self.char = loop, ble, char
        self.outgoing_packet_length = max_write
        self.incoming_packet_length = 512
        self.packets: queue.Queue[bytes] = queue.Queue()

    def write(self, data) -> None:
        future = asyncio.run_coroutine_threadsafe(
            self.ble.write_gatt_char(self.char, bytes(data), response=False), self.loop
        )
        future.result(timeout=15)

    def readinto(self, buf) -> int:
        packet = self.packets.get(timeout=15)
        if len(packet) > len(buf):
            self.packets.queue.appendleft(packet)  # Adafruit retries with a 512-byte buffer
            raise ValueError("buffer too small")
        buf[: len(packet)] = packet
        return len(packet)


def main() -> int:
    address = sys.argv[1]
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    async def connect():
        from bleak import BleakClient
        from bleak.backends.device import BLEDevice

        path = f"/org/bluez/hci0/dev_{address.upper().replace(':', '_')}"
        ble = BleakClient(BLEDevice(address.upper(), None, {"path": path, "props": None}))
        await ble.connect()
        char = ble.services.get_characteristic(fts.TRANSFER_UUID)
        size = min(getattr(char, "max_write_without_response_size", 20) or 20, 512)
        buffer = BleakPacketBuffer(loop, ble, char, size)
        await ble.start_notify(char, lambda _, data: buffer.packets.put(bytes(data)))
        return ble, buffer

    ble, buffer = asyncio.run_coroutine_threadsafe(connect(), loop).result(timeout=60)
    try:
        client = adafruit.FileTransferClient(types.SimpleNamespace(version=4, raw=buffer))
        data = os.urandom(50_000)
        client.mkdir("/fts-adafruit/")
        client.write("/fts-adafruit/a.bin", data)
        back = bytes(client.read("/fts-adafruit/a.bin"))
        listing = client.listdir("/fts-adafruit/")
        client.move("/fts-adafruit/a.bin", "/fts-adafruit/b.bin")
        moved = [name for name, *_ in client.listdir("/fts-adafruit/")]
        client.delete("/fts-adafruit/")
        gone = [name for name, *_ in client.listdir("/")]
        ok = back == data and [(n, s) for n, s, *_ in listing] == [("a.bin", 50_000)] and moved == ["b.bin"] and "fts-adafruit" not in gone
        print(json.dumps({
            "ok": ok,
            "sha256_sent": hashlib.sha256(data).hexdigest(),
            "sha256_read": hashlib.sha256(back).hexdigest(),
            "max_write": buffer.outgoing_packet_length,
        }))
        return 0 if ok else 1
    finally:
        watch_fs.keep_link(ble)
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    sys.exit(main())
