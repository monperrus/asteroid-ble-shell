"""Unit tests for the BLE UART client's pure helpers."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize(("reported_size", "first_chunk"), [(20, 20), (512, 512), (514, 512)])
def test_write_chunks_fit_the_gatt_attribute(reported_size: int, first_chunk: int) -> None:
    """BlueZ can report MTU minus headers, which can exceed the value limit."""
    import asyncio

    writes = []
    received = bytearray()
    characteristic = SimpleNamespace(max_write_without_response_size=reported_size)

    async def write(_characteristic, data: bytes, *, response: bool) -> None:
        assert not response
        writes.append(len(data))
        if len(data) <= 512:  # BlueZ silently drops oversized Write Commands.
            received.extend(data)

    client = SimpleNamespace(
        services=SimpleNamespace(get_characteristic=lambda _uuid: characteristic),
        write_gatt_char=write,
    )
    command = b"x" * 563 + b"\r"
    asyncio.run(ble_uart.RxWriter(client).send(command))
    assert writes[0] == first_chunk
    assert received == command


class _ProbeWatch:
    """Fake client + RX writer: the shell answers the probe number `answer_on` (1-based), or never."""

    def __init__(self, answer_on: int | None) -> None:
        self.answer_on, self.probes, self.calls = answer_on, 0, []
        self.answered = asyncio.Event()

    async def send(self, data: bytes) -> None:
        assert data == ble_uart.PROBE
        self.probes += 1
        self.calls.append("probe")
        if self.probes == self.answer_on:
            self.answered.set()

    async def stop_notify(self, uuid: str) -> None:
        self.calls.append("stop_notify")

    async def start_notify(self, uuid: str, callback) -> None:
        self.calls.append("start_notify")


@pytest.mark.parametrize(
    ("answer_on", "expected", "calls"),
    [
        (1, True, ["probe"]),
        (2, True, ["probe", "stop_notify", "start_notify", "probe"]),
        (None, False, ["probe", "stop_notify", "start_notify", "probe"]),
    ],
)
def test_probe_shell_resubscribes_once(monkeypatch, answer_on, expected, calls) -> None:
    monkeypatch.setattr(ble_uart, "PROBE_RETRY_AFTER", 0.05)
    monkeypatch.setattr(ble_uart, "SHELL_REAP_TIME", 0.01)
    watch = _ProbeWatch(answer_on)
    ok = asyncio.run(ble_uart.probe_shell(watch, watch, watch.answered, None, 0.2))
    assert (ok, watch.calls) == (expected, calls)


def test_probe_shell_without_time_for_a_retry(monkeypatch) -> None:
    monkeypatch.setattr(ble_uart, "PROBE_RETRY_AFTER", 0.05)
    watch = _ProbeWatch(None)
    assert not asyncio.run(ble_uart.probe_shell(watch, watch, watch.answered, None, 0.05))
    assert watch.calls == ["probe"]
