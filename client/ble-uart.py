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
PROBE = b"\r"  # an empty command line: the shell answers with its prompt
WRITE_TIMEOUT = 10.0  # seconds one chunk may take before the client fails loudly


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
    parser.add_argument(
        "--drain-timeout",
        type=float,
        default=1,
        help="maximum seconds to flush stdin after EOF in --line-mode (default: 1)",
    )
    parser.add_argument(
        "--arm-timeout",
        type=float,
        default=5,
        help="seconds to wait for the shell's first prompt before reporting "
        "that BLE UART is not armed (default: 5)",
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


class RxWriter:
    """Writes to the watch's NUS RX characteristic, failing loudly when stalled.

    Bleak awaits each write's D-Bus reply with no timeout of its own, so every
    chunk is bounded by WRITE_TIMEOUT.
    """

    def __init__(self, client: BleakClient) -> None:
        self.client = client
        char = client.services.get_characteristic(NUS_RX_UUID)
        if char is None:
            raise RuntimeError("NUS is incomplete: RX characteristic is absent")
        self.char = char
        size = getattr(self.char, "max_write_without_response_size", None)
        self.chunk_size = size if isinstance(size, int) and size > 0 else FALLBACK_CHUNK_SIZE

    async def send(self, data: bytes) -> None:
        for start in range(0, len(data), self.chunk_size):
            try:
                await asyncio.wait_for(
                    self.client.write_gatt_char(
                        self.char, data[start : start + self.chunk_size], response=False
                    ),
                    timeout=WRITE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"write to the watch stalled for {WRITE_TIMEOUT:g} s "
                    f"({len(data) - start} bytes of this line unsent)"
                ) from None


async def write_stream(
    writer: RxWriter, queue: asyncio.Queue[bytes], stop: asyncio.Event, failure: list[BaseException]
) -> None:
    # Runs until cancelled, not until `stop`: line mode sets `stop` at stdin
    # EOF, while lines read before EOF are still queued and must be sent.
    while True:
        data = await queue.get()
        try:
            await writer.send(data)
        except Exception as error:
            # Surface the error from run() instead of leaving the queue to drain
            # into a silent timeout.
            failure.append(error)
            stop.set()
            raise
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


def to_terminal_line(line: bytes) -> bytes:
    """Turn a stdin line ending (LF or CRLF) into the CR a terminal Enter sends.

    The watch shell reads a PTY through readline: with LF endings only the
    first line of a batch runs and the following ones are lost.
    """
    if line.endswith(b"\r\n"):
        return line[:-2] + b"\r"
    if line.endswith(b"\n"):
        return line[:-1] + b"\r"
    return line


async def line_mode_input(queue: asyncio.Queue[bytes], stop: asyncio.Event) -> None:
    while not stop.is_set():
        line = await asyncio.to_thread(sys.stdin.buffer.readline)
        if not line:
            stop.set()
            return
        await queue.put(to_terminal_line(line))


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

        answered = asyncio.Event()

        def on_tx(_, data: bytearray) -> None:
            answered.set()
            write_stdout(bytearray(data))

        await client.start_notify(NUS_TX_UUID, on_tx)
        rx = RxWriter(client)
        print(
            "Connected to ceres@watch over BLE UART. Press Ctrl-] to disconnect.",
            file=sys.stderr,
        )
        # The watch drops input silently while BLE UART is disarmed, so probe
        # before reading stdin and fail loudly when the shell stays mute.
        await rx.send(PROBE)
        try:
            await asyncio.wait_for(answered.wait(), timeout=args.arm_timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"no answer from the watch shell within {args.arm_timeout:g} s: "
                "BLE UART is not armed (open BLE SHELL on the watch and tap "
                "until it says SHELL ARMED)"
            ) from None

        failure: list[BaseException] = []
        writer = asyncio.create_task(write_stream(rx, input_queue, stop, failure))
        reader = asyncio.create_task(
            line_mode_input(input_queue, stop)
            if args.line_mode
            else raw_terminal_input(input_queue, stop)
        )
        await stop.wait()
        # Let bytes already accepted from stdin reach the watch before a local
        # EOF/Ctrl-] ends the process. A disconnected client cannot drain, so
        # keep this bounded, and say so when input is left behind.
        unsent = 0
        if client.is_connected and not failure:
            try:
                await asyncio.wait_for(input_queue.join(), timeout=args.drain_timeout)
            except asyncio.TimeoutError:
                unsent = input_queue.qsize()
            if args.line_mode and args.line_settle > 0 and not failure:
                await asyncio.sleep(args.line_settle)
        for task in (reader, writer):
            task.cancel()
        for task in (reader, writer):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if failure:
            raise RuntimeError(str(failure[0])) from failure[0]
        if unsent or not input_queue.empty():
            raise RuntimeError(
                f"{max(unsent, input_queue.qsize())} input line(s) not sent to the watch "
                f"(drain timeout {args.drain_timeout:g} s, or the watch disconnected)"
            )
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
