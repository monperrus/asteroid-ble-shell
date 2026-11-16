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
