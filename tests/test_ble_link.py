"""ble_link: leave the watch's link as it was found; retry InProgress."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import ClassVar

import pytest

pytest.importorskip("bleak")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "client"))
import ble_link
from bleak.exc import BleakDBusError


class FakeBackend:
    def __init__(self) -> None:
        self._disconnect_monitor_event = asyncio.Event()


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []
    failures: ClassVar[list[str]] = []

    def __init__(self, target, **kwargs) -> None:
        self.target, self.kwargs = target, kwargs
        self._backend = FakeBackend()
        self.connects = 0
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self, **kwargs) -> None:
        self.connects += 1
        if FakeClient.failures:
            raise BleakDBusError(FakeClient.failures.pop(0), [])

    async def disconnect(self) -> None:
        self.disconnected = True
        raise EOFError("spurious, as on this host")


@pytest.fixture
def fake(monkeypatch):
    FakeClient.instances.clear()
    FakeClient.failures = []
    monkeypatch.setattr(ble_link, "BleakClient", FakeClient)
    monkeypatch.setattr(ble_link, "RETRY_DELAY", 0)
    monkeypatch.setattr(ble_link, "OTHER_CONNECT_WAIT", 0.2)
    # connect_errors: answers of successive Device1.Connect calls (None = success);
    # other_up_after: the other client's link comes up after this many Connected reads.
    state = {"connected": False, "connect_errors": [], "connects": 0, "other_up_after": None, "reads": 0}

    async def bluez_connected(path: str) -> bool:
        state["path"] = path
        state["reads"] += 1
        if state["other_up_after"] is not None and state["reads"] > state["other_up_after"]:
            state["connected"] = True
        return state["connected"]

    async def bluez_connect(path: str, timeout: float) -> str | None:
        state["connects"] += 1
        error = state["connect_errors"].pop(0) if state["connect_errors"] else None
        if error is None:
            state["connected"] = True
        return error

    monkeypatch.setattr(ble_link, "bluez_connected", bluez_connected)
    monkeypatch.setattr(ble_link, "bluez_connect", bluez_connect)
    return state


async def session(**kwargs) -> FakeClient:
    async with ble_link.WatchLink("43:43:a1:12:1f:ac", "hci1", **kwargs) as client:
        assert client.connects >= 1
    return client


def test_existing_link_is_kept(fake) -> None:
    fake["connected"] = True
    client = asyncio.run(session())
    assert not client.disconnected
    assert client._backend._disconnect_monitor_event.is_set()  # Bleak's exit-time Disconnect disarmed
    assert fake["path"] == "/org/bluez/hci1/dev_43_43_A1_12_1F_AC"
    assert client.target.details["path"] == fake["path"]


def test_link_opened_here_is_closed_despite_eoferror(fake) -> None:
    client = asyncio.run(session())
    assert fake["connects"] == 1
    assert client.disconnected


def test_existing_link_is_not_connected_again(fake) -> None:
    fake["connected"] = True
    asyncio.run(session())
    assert fake["connects"] == 0


def test_another_clients_connect_is_waited_out_not_cancelled(fake) -> None:
    # watch-autolock is reconnecting: Connect answers InProgress until its link is up.
    fake["connect_errors"] = ["org.bluez.Error.InProgress"] * 100
    fake["other_up_after"] = 3
    client = asyncio.run(session())
    assert fake["connects"] == 2  # InProgress twice, then the other link is seen up
    assert not client.disconnected  # the link is watch-autolock's
    assert client._backend._disconnect_monitor_event.is_set()


def test_failed_raw_connect_raises(fake) -> None:
    fake["connect_errors"] = ["org.bluez.Error.Failed"]
    with pytest.raises(BleakDBusError):
        asyncio.run(session())
    fake["connect_errors"] = ["org.bluez.Error.InProgress"] * 10**6
    with pytest.raises(BleakDBusError):
        asyncio.run(session())


def test_inprogress_is_retried_and_the_link_left_to_its_opener(fake) -> None:
    # watch-autolock was reconnecting: the link it opens must survive this session.
    FakeClient.failures = ["org.bluez.Error.InProgress"] * 3
    client = asyncio.run(session())
    assert client.connects == 4
    assert not client.disconnected
    assert client._backend._disconnect_monitor_event.is_set()


def test_other_errors_and_endless_inprogress_raise(fake) -> None:
    FakeClient.failures = ["org.bluez.Error.Failed"]
    with pytest.raises(BleakDBusError):
        asyncio.run(session())
    FakeClient.failures = ["org.bluez.Error.InProgress"] * 10**6
    with pytest.raises(BleakDBusError):
        asyncio.run(session())


def test_client_kwargs_pass_through(fake) -> None:
    def callback(_):
        return None

    client = asyncio.run(session(disconnected_callback=callback))
    assert client.kwargs["disconnected_callback"] is callback
    assert client.kwargs["bluez"] == {"adapter": "hci1"}
