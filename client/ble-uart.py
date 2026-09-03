#!/usr/bin/env python3
"""SSH-like interactive terminal client for AsteroidOS's Nordic UART Service.

The watch is deliberately a secure, manually armed BLE peripheral.  This
client only connects to an already paired/trusted watch; it cannot arm it.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import termios
import tty
from bleak import BleakClient, BleakError, BleakScanner

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
LOCAL_ESCAPE = b"\x1d"  # Ctrl-]
FALLBACK_CHUNK_SIZE = 20


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Open an interactive shell through AsteroidOS BLE UART (NUS)."
    )
    parser.add_argument("watch", help="paired watch address or advertised name")
    parser.add_argument("--adapter", metavar="HCI", help="BlueZ adapter, e.g. hci1")
    parser.add_argument(
        "--line-mode",
        action="store_true",
        help="script mode: read stdin line by line instead of opening the shell REPL",
    )
    parser.add_argument("--scan-timeout", type=float, default=15, help="name lookup timeout")
    parser.add_argument(
        "--line-settle",
        type=float,
        default=0.5,
        help="seconds to receive output after stdin EOF in --line-mode (default: 0.5)",
    )
    return parser.parse_args()


def bluez_kwargs(adapter: str | None) -> dict:
    """Return BlueZ backend options without imposing an adapter by default."""
    # Bleak 1.x moved the adapter option under its BlueZ backend dictionary.
    # Using this form works for both scanner and client and avoids the
    # deprecated top-level ``adapter=`` warning.
    return {"bluez": {"adapter": adapter}} if adapter else {}


async def resolve_watch(name_or_address: str, adapter: str | None, timeout: float):
    if ":" in name_or_address:
        return name_or_address

    device = await BleakScanner.find_device_by_filter(
        lambda dev, adv: name_or_address.casefold()
        in ((dev.name or adv.local_name or "").casefold()),
        timeout=timeout,
        **bluez_kwargs(adapter),
    )
    if device is None:
        raise RuntimeError(f"watch named {name_or_address!r} was not found during scan")
    return device


def write_stdout(data: bytearray) -> None:
    # os.write preserves bytes (including ANSI controls and NUL) and avoids
    # Python text encoding/newline conversion.
    view = memoryview(data)
    while view:
        written = os.write(sys.stdout.fileno(), view)
        view = view[written:]


def tx_chunk_size(client: BleakClient) -> int:
    """Use Bleak's negotiated write limit when exposed; otherwise stay safe."""
    for service in client.services:
        for characteristic in service.characteristics:
            if characteristic.uuid.lower() == NUS_RX_UUID:
                size = characteristic.max_write_without_response_size
                if isinstance(size, int) and size > 0:
                    return size
    return FALLBACK_CHUNK_SIZE


async def write_stream(client: BleakClient, queue: asyncio.Queue[bytes], stop: asyncio.Event) -> None:
    chunk_size = tx_chunk_size(client)
    while not stop.is_set():
        data = await queue.get()
        try:
            for start in range(0, len(data), chunk_size):
                await client.write_gatt_char(
                    NUS_RX_UUID, data[start : start + chunk_size], response=False
                )
        finally:
            queue.task_done()


async def raw_terminal_input(queue: asyncio.Queue[bytes], stop: asyncio.Event) -> None:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    loop = asyncio.get_running_loop()

    def on_input() -> None:
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            return
        if not data:
            stop.set()
            return
        if LOCAL_ESCAPE in data:
            before, _, _ = data.partition(LOCAL_ESCAPE)
            if before:
                queue.put_nowait(before)
            write_stdout(bytearray(b"\r\n[BLE UART disconnected]\r\n"))
            stop.set()
            return
        queue.put_nowait(data)

    tty.setraw(fd)
    loop.add_reader(fd, on_input)
    try:
        await stop.wait()
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


async def line_mode_input(queue: asyncio.Queue[bytes], stop: asyncio.Event) -> None:
    while not stop.is_set():
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            stop.set()
            return
        await queue.put(line)


def explain_error(error: Exception) -> str:
    text = str(error)
    lower = text.casefold()
    if "not found" in lower:
        return f"watch not found: {text}"
    if "not permitted" in lower or "not authorized" in lower or "authentication" in lower:
        return f"authorization failed (pair and trust the host on the watch): {text}"
    if "not connected" in lower or "disconnected" in lower:
        return f"watch disconnected: {text}"
    return text


async def run(args: argparse.Namespace) -> None:
    if not args.line_mode and not sys.stdin.isatty():
        raise RuntimeError("the shell REPL needs a terminal; use --line-mode for a pipe or file")
    target = await resolve_watch(args.watch, args.adapter, args.scan_timeout)
    stop = asyncio.Event()
    input_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=128)

    def disconnected(_: BleakClient) -> None:
        if not stop.is_set():
            print("\r\nBLE UART: watch disconnected", file=sys.stderr)
            stop.set()

    client = BleakClient(
        target, disconnected_callback=disconnected, **bluez_kwargs(args.adapter)
    )
    try:
        await client.connect()
        if not client.is_connected:
            raise RuntimeError("connection failed")

        if not any(service.uuid.lower() == NUS_SERVICE_UUID for service in client.services):
            raise RuntimeError(
                "NUS service is absent (wrong watch, or btsyncd/GATT registration failed)"
            )

        found = {characteristic.uuid.lower() for service in client.services for characteristic in service.characteristics}
        if NUS_RX_UUID not in found or NUS_TX_UUID not in found:
            raise RuntimeError("NUS is incomplete: RX or TX characteristic is absent")

        await client.start_notify(NUS_TX_UUID, lambda _, data: write_stdout(bytearray(data)))
        print(
            "Connected to ceres@watch over BLE UART. Press Ctrl-] to disconnect.",
            file=sys.stderr,
        )

        writer = asyncio.create_task(write_stream(client, input_queue, stop))
        reader = asyncio.create_task(
            line_mode_input(input_queue, stop)
            if args.line_mode
            else raw_terminal_input(input_queue, stop)
        )
        await stop.wait()
        # Let bytes already accepted from stdin reach the watch before a local
        # EOF/Ctrl-] ends the process. A disconnected client cannot drain, so
        # keep this bounded.
        if client.is_connected:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(input_queue.join(), timeout=1)
            if args.line_mode and args.line_settle > 0:
                await asyncio.sleep(args.line_settle)
        for task in (reader, writer):
            task.cancel()
        for task in (reader, writer):
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        # Deliberately do not await client.disconnect().  On this host's BlueZ
        # stack, disconnect can race its D-Bus Disconnected signal and report
        # EOFError after a successful session. Process teardown closes the
        # connection; the watch then disarms and reaps the shell.
        pass


def main() -> int:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except (BleakError, OSError, RuntimeError) as error:
        print(f"BLE UART: {explain_error(error)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
