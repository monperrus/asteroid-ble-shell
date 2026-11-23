"""App bundles: strict format, atomic install, rollback, incoming queue."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deploy"))
import app_bundle
import asteroid_app_install as installer
from app_bundle import BundleError

DESKTOP = app_bundle.desktop_entry("demo", "Demo", "ios-apps-outline", "main.qml").encode()


def files(version: str = "1", **extra) -> dict[str, tuple[bytes, int]]:
    out = {
        "main.qml": (f"import QtQuick\nItem {{ property int v: {version} }}\n".encode(), 0o644),
        "lib/helper.js": (b"function f() { return 1 }\n", 0o644),
        "bin/tool": (b"#!/bin/sh\necho hi\n", 0o755),
        "launcher.desktop": (DESKTOP, 0o644),
    }
    out.update(extra)
    return out


def bundle(version: str = "1", **extra) -> bytes:
    return app_bundle.pack("demo", files(version, **extra))


# -- format -----------------------------------------------------------------------------


def test_pack_is_deterministic_and_parses() -> None:
    data = bundle()
    assert data == bundle()
    parsed = app_bundle.parse(data)
    assert parsed.app_id == "demo"
    assert [e.path for e in parsed.entries] == ["bin/tool", "launcher.desktop", "lib/helper.js", "main.qml"]
    assert parsed.content(parsed.entries[-1]).startswith(b"import QtQuick")
    assert data[:8] == b"ASTDFU1\0" and data.count(b"\n", 48, 48 + int.from_bytes(data[12:16], "little")) == 1


def tamper(data: bytes, at: int) -> bytes:
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d[:-1], "payload size"),
        (lambda d: d + b"x", "payload size"),
        (lambda d: tamper(d, len(d) - 1), "SHA-256 mismatch"),
        (lambda d: tamper(d, 60), "manifest SHA-256"),
        (lambda d: tamper(d, 0), "not an asteroid-app-bundle"),
        (lambda d: d[:20], "shorter than the header"),
        (lambda d: d[:60], "manifest length"),
    ],
)
def test_parse_rejects_damage(mutate, message: str) -> None:
    with pytest.raises(BundleError, match=message):
        app_bundle.parse(mutate(bundle()))


@pytest.mark.parametrize(
    "bad",
    [
        {"../evil": (b"x", 0o644)},
        {"/abs": (b"x", 0o644)},
        {"a//b": (b"x", 0o644)},
        {"a/./b": (b"x", 0o644)},
        {"a\\b": (b"x", 0o644)},
        {"tab\there": (b"x", 0o644)},
        {"setuid": (b"x", 0o4755)},
        {"main.qml/inner": (b"x", 0o644)},  # file/directory prefix conflict
        {"café": (b"x", 0o644)},  # not NFC
    ],
)
def test_pack_refuses_invalid_entries(bad) -> None:
    with pytest.raises(BundleError):
        app_bundle.pack("demo", files() | bad)


def test_desktop_entry_and_app_id_are_mandatory() -> None:
    without = {k: v for k, v in files().items() if k != "launcher.desktop"}
    with pytest.raises(BundleError, match="launcher.desktop is missing"):
        app_bundle.pack("demo", without)
    with pytest.raises(BundleError, match="app_id"):
        app_bundle.pack("Demo_App", files())


def test_read_app_generates_launcher_entry(tmp_path: Path) -> None:
    app = tmp_path / "app-tabata"
    app.mkdir()
    (app / "tabata.qml").write_text("Item {}")
    (app / ".hidden").write_text("skip")
    app_id, collected = app_bundle.read_app(app, name="Tabata", icon="ios-flame-outline")
    assert app_id == "tabata"
    assert sorted(collected) == ["launcher.desktop", "tabata.qml"]
    entry = collected["launcher.desktop"][0].decode()
    assert "Exec=/home/ceres/.local/bin/qml-run /home/ceres/.local/share/asteroid-deploy/apps/tabata/current/tabata.qml" in entry
    assert "Name=Tabata" in entry and "Icon=ios-flame-outline" in entry


# -- installer --------------------------------------------------------------------------


class Launcher:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls = ok, 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.ok


def put(home: Path, data: bytes, name: str = "demo.aab") -> Path:
    path = home / name
    path.write_bytes(data)
    return path


def app_dir(home: Path) -> Path:
    return home / ".local/share/asteroid-deploy/apps/demo"


def active_main(home: Path) -> bytes:
    return (app_dir(home) / "current/main.qml").read_bytes()


def test_first_install_publishes_release_link_and_launcher_entry(tmp_path: Path) -> None:
    launcher = Launcher()
    result = installer.install(put(tmp_path, bundle()), tmp_path, launcher)
    sha = app_bundle.parse(bundle()).manifest_sha256
    assert result == {"status": "installed", "app_id": "demo", "release": sha, "previous": None}
    assert os.readlink(app_dir(tmp_path) / "current") == f"releases/{sha}"
    entry = tmp_path / ".local/share/applications/demo.desktop"
    assert entry.read_bytes() == DESKTOP
    assert os.readlink(entry) == "../asteroid-deploy/apps/demo/current/launcher.desktop"
    release = app_dir(tmp_path) / "releases" / sha
    assert oct((release / "bin/tool").stat().st_mode & 0o777) == "0o755"
    assert oct((release / "main.qml").stat().st_mode & 0o777) == "0o644"
    assert (tmp_path / ".local/bin/qml-run").stat().st_mode & 0o111
    assert launcher.calls == 1


def test_update_switches_atomically_and_keeps_one_previous_release(tmp_path: Path) -> None:
    for version in ("1", "2", "3"):
        installer.install(put(tmp_path, bundle(version)), tmp_path, Launcher())
    assert b"v: 3" in active_main(tmp_path)
    releases = sorted(p.name for p in (app_dir(tmp_path) / "releases").iterdir())
    expected = sorted(app_bundle.parse(bundle(v)).manifest_sha256 for v in ("2", "3"))
    assert releases == expected


def test_reinstalling_the_same_bundle_reuses_the_release(tmp_path: Path) -> None:
    installer.install(put(tmp_path, bundle()), tmp_path, Launcher())
    result = installer.install(put(tmp_path, bundle()), tmp_path, Launcher())
    assert result["release"] == result["previous"]


def test_tampered_existing_release_is_not_reused(tmp_path: Path) -> None:
    installer.install(put(tmp_path, bundle()), tmp_path, Launcher())
    sha = app_bundle.parse(bundle()).manifest_sha256
    (app_dir(tmp_path) / "releases" / sha / "main.qml").write_bytes(b"tampered")
    with pytest.raises(BundleError, match="content differs"):
        installer.install(put(tmp_path, bundle()), tmp_path, Launcher())


def test_launcher_failure_rolls_back_to_previous_release(tmp_path: Path) -> None:
    installer.install(put(tmp_path, bundle("1")), tmp_path, Launcher())
    failing = Launcher(ok=False)
    with pytest.raises(RuntimeError, match="previous release is active"):
        installer.install(put(tmp_path, bundle("2")), tmp_path, failing)
    assert b"v: 1" in active_main(tmp_path)
    assert failing.calls == 2  # the failed restart, then the restart of the old release


def test_first_install_launcher_failure_restores_old_entry(tmp_path: Path) -> None:
    entry = tmp_path / ".local/share/applications/demo.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nName=old qml-run deployment\n")
    with pytest.raises(RuntimeError):
        installer.install(put(tmp_path, bundle()), tmp_path, Launcher(ok=False))
    assert not (app_dir(tmp_path) / "current").exists()
    assert not entry.is_symlink() and "old qml-run" in entry.read_text()


def test_bad_bundle_leaves_current_release_untouched(tmp_path: Path) -> None:
    installer.install(put(tmp_path, bundle("1")), tmp_path, Launcher())
    before = {p: p.read_bytes() for p in (app_dir(tmp_path) / "current").rglob("*") if p.is_file()}
    with pytest.raises(BundleError):
        installer.install(put(tmp_path, bundle("2")[:-1]), tmp_path, Launcher())
    after = {p: p.read_bytes() for p in (app_dir(tmp_path) / "current").rglob("*") if p.is_file()}
    assert before == after
    assert not [p for p in (app_dir(tmp_path) / "releases").iterdir() if p.name.startswith(".")]


def test_exec_must_point_inside_the_bundle(tmp_path: Path) -> None:
    desktop = app_bundle.desktop_entry("demo", "Demo", "x", "missing.qml").encode()
    data = app_bundle.pack("demo", files(**{"launcher.desktop": (desktop, 0o644)}))
    with pytest.raises(BundleError, match="does not contain"):
        installer.install(put(tmp_path, data), tmp_path, Launcher())


def test_incoming_queue_writes_results_and_consumes_bundles(tmp_path: Path) -> None:
    incoming = tmp_path / ".local/share/asteroid-deploy/incoming"
    incoming.mkdir(parents=True)
    (incoming / "good.aab").write_bytes(bundle())
    (incoming / "bad.aab").write_bytes(b"garbage")
    (incoming / "upload.part").write_bytes(b"still uploading")
    results = installer.process_incoming(tmp_path, Launcher())
    assert [r["status"] for r in results] == ["error", "installed"]
    assert sorted(p.name for p in incoming.iterdir()) == ["bad.result", "good.result", "upload.part"]
    good = json.loads((incoming / "good.result").read_text())
    assert good["status"] == "installed" and good["bundle"] == "good.aab"
    assert "BundleError" in json.loads((incoming / "bad.result").read_text())["error"]


def test_cli_installs_one_bundle(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(installer, "restart_launcher", Launcher())
    monkeypatch.setattr(installer.install, "__defaults__", (installer.restart_launcher,))
    assert installer.main([str(put(tmp_path, bundle())), "--home", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "installed"


def test_manifest_hash_names_the_release(tmp_path: Path) -> None:
    data = bundle()
    manifest_len = int.from_bytes(data[12:16], "little")
    assert app_bundle.parse(data).manifest_sha256 == hashlib.sha256(data[48 : 48 + manifest_len]).hexdigest()
