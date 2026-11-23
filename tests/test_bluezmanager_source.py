"""Regression checks for BLE lifecycle notifications."""

from __future__ import annotations

from pathlib import Path


def _function_body(source: str, signature: str) -> str:
    """Return the brace-delimited body of a C++ function."""
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"unterminated function: {signature}")


def test_connection_changes_do_not_create_watch_notifications() -> None:
    source = (Path(__file__).parent.parent / "server" / "src" / "bluezmanager.cpp").read_text()
    handler = _function_body(source, "void BlueZManager::onConnectedChanged()")

    assert '"Notify"' not in handler
    assert "mUartSession->endPeer()" in handler
