"""Unit tests for the BLE UART client's pure helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("bleak")
_spec = importlib.util.spec_from_file_location(
    "ble_uart", Path(__file__).parent.parent / "client" / "ble-uart.py"
)
ble_uart = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ble_uart)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (b"uname -a\n", b"uname -a\r"),
        (b"uname -a\r\n", b"uname -a\r"),
        (b"uname -a\r", b"uname -a\r"),
        (b"uname -a", b"uname -a"),
        (b"\n", b"\r"),
    ],
)
def test_to_terminal_line(line: bytes, expected: bytes) -> None:
    assert ble_uart.to_terminal_line(line) == expected


class _FakeWriter:
    def __init__(self, hang_on: bytes | None = None) -> None:
        self.sent: list[bytes] = []
        self.hang_on = hang_on

    async def send(self, data: bytes) -> None:
        if data == self.hang_on:
            raise RuntimeError("write to the watch stalled")
        self.sent.append(data)


def test_write_stream_sends_lines_queued_before_eof() -> None:
    """Line mode sets `stop` at stdin EOF; queued lines must still go out."""
    import asyncio

    async def scenario() -> list[bytes]:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        stop = asyncio.Event()
        for line in (b"a\r", b"b\r", b"c\r"):
            queue.put_nowait(line)
        stop.set()  # EOF already reached
        writer = _FakeWriter()
        task = asyncio.create_task(ble_uart.write_stream(writer, queue, stop, []))
        await asyncio.wait_for(queue.join(), timeout=1)
        task.cancel()
        return writer.sent

    assert asyncio.run(scenario()) == [b"a\r", b"b\r", b"c\r"]


def test_write_stream_reports_a_stalled_write() -> None:
    import asyncio

    async def scenario() -> tuple[list[BaseException], bool]:
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        stop = asyncio.Event()
        failure: list[BaseException] = []
        queue.put_nowait(b"x\r")
        task = asyncio.create_task(ble_uart.write_stream(_FakeWriter(b"x\r"), queue, stop, failure))
        await asyncio.wait_for(stop.wait(), timeout=1)
        task.cancel()
        return failure, stop.is_set()

    failure, stopped = asyncio.run(scenario())
    assert stopped and "stalled" in str(failure[0])
