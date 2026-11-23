"""watch-fs commands against the server model (no Bluetooth)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest
from test_fts import Link

_spec = importlib.util.spec_from_file_location(
    "watch_fs", Path(__file__).resolve().parent.parent / "client" / "watch-fs.py"
)
watch_fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(watch_fs)


@pytest.fixture
def env(tmp_path: Path, monkeypatch, make_server):
    root, local = tmp_path / "watch", tmp_path / "host"
    root.mkdir()
    local.mkdir()
    monkeypatch.chdir(local)
    link = Link(make_server(root, notify_size=64, free_space=512), 64)

    def cli(*argv: str):
        args = watch_fs.parse_args(["AA:BB:CC:DD:EE:FF", *argv])
        result = asyncio.run(watch_fs.run(args, link.client))
        watch_fs.render(args, result)
        return result

    return cli, root, local


def test_put_get_ls_mv_rm(env, capsys) -> None:
    cli, root, local = env
    data = os.urandom(3_000)
    (local / "trace.bin").write_bytes(data)
    cli("mkdir", "/traces")
    assert cli("put", "trace.bin", "/traces/", "--verify")["verified"] is True
    assert (root / "traces/trace.bin").read_bytes() == data
    capsys.readouterr()
    cli("ls", "/traces")
    assert capsys.readouterr().out.rstrip().endswith("trace.bin")
    cli("--json", "ls", "/")
    assert json.loads(capsys.readouterr().out) == [
        {"name": "traces", "type": "dir", "size": 0, "mtime_ns": (root / "traces").stat().st_mtime_ns}
    ]
    cli("mv", "/traces/trace.bin", "/traces/t.bin")
    result = cli("get", "/traces/t.bin")
    assert (local / "t.bin").read_bytes() == data and result["size"] == 3_000
    cli("rm", "/traces")
    assert list(root.iterdir()) == []
    assert capsys.readouterr().out == ""  # silent on success


def test_put_preserves_local_mtime(env) -> None:
    cli, root, local = env
    (local / "a").write_bytes(b"x")
    os.utime(local / "a", ns=(1_600_000_000_000_000_000,) * 2)
    cli("put", "a", "/a")
    assert (root / "a").stat().st_mtime_ns == 1_600_000_000_000_000_000


def test_relative_remote_path_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as raised:
        watch_fs.parse_args(["AA:BB:CC:DD:EE:FF", "ls", "traces"])
    assert raised.value.code == 2


def test_main_reports_refusal_with_status(env, monkeypatch, capsys) -> None:
    _ = env  # chdir into the host dir

    async def fake_connect(args):
        raise watch_fs.fts.FtsError("read", "/missing", watch_fs.fts.ERROR_NO_FILE)

    monkeypatch.setattr(watch_fs, "connect_and_run", fake_connect)
    assert watch_fs.main(["AA:BB:CC:DD:EE:FF", "--json", "get", "/missing"]) == 1
    out = capsys.readouterr()
    assert json.loads(out.out)["status"] == 3
    assert "no such file" in out.err
