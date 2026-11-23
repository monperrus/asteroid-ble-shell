#!/usr/bin/env python3
"""Files on an AsteroidOS watch over BLE (Adafruit File Transfer, service 0xFEBB).

Remote paths are absolute and rooted at the watch user's home: ``/`` is
``/home/ceres``. The host must already be paired and trusted with the watch.

Silent on success except for ``ls`` and ``get -``; ``--json`` prints one JSON
document per command. Exit status: 0 success, 1 the watch refused or the link
failed, 2 usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fts

MAX_ATTRIBUTE_VALUE_SIZE = 512
FALLBACK_WRITE_SIZE = 20


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="watch-fs",
        description="Read and write files on an AsteroidOS watch over BLE. "
        "Remote '/' is /home/ceres.",
    )
    parser.add_argument("watch", help="paired watch address or advertised name")
    parser.add_argument("--adapter", metavar="HCI", help="BlueZ adapter, e.g. hci1")
    parser.add_argument("--scan-timeout", type=float, default=15, help="name lookup timeout (s)")
    parser.add_argument("--timeout", type=float, default=15, help="max wait for each reply (s)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ls", help="list a directory")
    p.add_argument("path", nargs="?", default="/")
    p = sub.add_parser("get", help="download a file ('-' writes to stdout)")
    p.add_argument("remote")
    p.add_argument("local", nargs="?", help="default: basename of REMOTE")
    p = sub.add_parser("put", help="upload a file")
    p.add_argument("local")
    p.add_argument("remote", help="destination file path (a trailing / appends the basename)")
    p.add_argument("--verify", action="store_true", help="read the file back and compare SHA-256")
    p = sub.add_parser("rm", help="delete a file or a directory recursively")
    p.add_argument("path")
    p = sub.add_parser("mkdir", help="create a directory and its parents")
    p.add_argument("path")
    p = sub.add_parser("mv", help="move or rename")
    p.add_argument("old")
    p.add_argument("new")
    args = parser.parse_args(argv)
    for name in ("path", "remote", "old", "new"):
        value = getattr(args, name, None)
        if value is not None and not value.startswith("/"):
            parser.error(f"remote path {value!r} must be absolute (remote / is /home/ceres)")
    return args


def remote_target(local: str, remote: str) -> str:
    return remote + Path(local).name if remote.endswith("/") else remote


def local_target(remote: str, local: str | None) -> str:
    return local if local else (Path(remote.rstrip("/")).name or "download")


async def run(args: argparse.Namespace, client: fts.Client) -> object:
    """Execute one command; return the JSON-able result (None when there is nothing to say)."""
    if args.command == "ls":
        entries = await client.listdir(args.path)
        return [
            {"name": e.name, "type": "dir" if e.is_dir else "file", "size": e.size, "mtime_ns": e.mtime_ns}
            for e in entries
        ]
    if args.command == "get":
        data = await client.read(args.remote)
        if args.local == "-":
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            return None
        target = local_target(args.remote, args.local)
        Path(target).write_bytes(data)
        return {"local": target, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if args.command == "put":
        data = Path(args.local).read_bytes()
        target = remote_target(args.local, args.remote)
        start = time.monotonic()
        await client.write(target, data, mtime_ns=Path(args.local).stat().st_mtime_ns)
        result = {
            "remote": target,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "seconds": round(time.monotonic() - start, 3),
        }
        if args.verify:
            back = await client.read(target)
            if back != data:
                raise fts.ProtocolError(
                    f"verify {target}: read back {len(back)} bytes, sha256 "
                    f"{hashlib.sha256(back).hexdigest()}, expected {result['sha256']}"
                )
            result["verified"] = True
        return result
    if args.command == "rm":
        await client.delete(args.path)
    elif args.command == "mkdir":
        await client.mkdir(args.path)
    elif args.command == "mv":
        await client.move(args.old, args.new)
    return None


def render(args: argparse.Namespace, result: object) -> None:
    if args.json:
        if result is not None:
            print(json.dumps(result))
        return
    if args.command == "ls":
        for e in result:  # type: ignore[union-attr]
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime_ns"] / 1e9))
            name = e["name"] + ("/" if e["type"] == "dir" else "")
            print(f"{e['size']:>10}  {stamp}  {name}")


def bluez_kwargs(adapter: str | None) -> dict:
    return {"bluez": {"adapter": adapter}} if adapter else {}


async def connect_and_run(args: argparse.Namespace) -> object:
    from bleak import BleakClient, BleakScanner

    target: object = args.watch
    if ":" not in args.watch:
        target = await BleakScanner.find_device_by_filter(
            lambda dev, adv: args.watch.casefold() in (dev.name or adv.local_name or "").casefold(),
            timeout=args.scan_timeout,
            **bluez_kwargs(args.adapter),
        )
        if target is None:
            raise RuntimeError(f"watch named {args.watch!r} was not found during scan")
    async with BleakClient(target, **bluez_kwargs(args.adapter)) as ble:
        char = ble.services.get_characteristic(fts.TRANSFER_UUID)
        if char is None:
            raise RuntimeError("watch does not offer the file transfer service (0xFEBB)")
        version_char = ble.services.get_characteristic(fts.VERSION_UUID)
        if version_char is not None:
            raw = await ble.read_gatt_char(version_char)
            version = struct.unpack("<I", bytes(raw).ljust(4, b"\0")[:4])[0]
            if version < fts.PROTOCOL_VERSION:
                raise RuntimeError(f"watch speaks file transfer v{version}, need v{fts.PROTOCOL_VERSION}")
        size = getattr(char, "max_write_without_response_size", None)
        if not isinstance(size, int) or size <= 0:
            size = FALLBACK_WRITE_SIZE
        size = min(size, MAX_ATTRIBUTE_VALUE_SIZE)

        async def send(data: bytes) -> None:
            await asyncio.wait_for(ble.write_gatt_char(char, data, response=False), args.timeout)

        client = fts.Client(send, size, timeout=args.timeout)
        await ble.start_notify(char, lambda _, data: client.on_notify(data))
        return await run(args, client)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = asyncio.run(connect_and_run(args))
    except (fts.FtsError, fts.ProtocolError, TimeoutError, OSError, RuntimeError) as error:
        if args.json:
            print(json.dumps({"error": str(error), "status": getattr(error, "status", None)}))
        print(f"watch-fs: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 -- Bleak raises its own types (BleakError, D-Bus errors)
        print(f"watch-fs: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    render(args, result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
