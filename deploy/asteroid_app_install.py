#!/usr/bin/env python3
"""Install AsteroidOS app bundles (.aab) atomically as the watch user.

    asteroid_app_install.py --incoming     # what asteroid-app-deploy.service runs
    asteroid_app_install.py BUNDLE.aab     # install one bundle by hand

Layout under ``~/.local/share/asteroid-deploy``:

    incoming/<name>.aab      dropped by a client (e.g. watch-fs install over BLE)
    incoming/<name>.result   JSON verdict written here, the .aab is removed
    apps/<app_id>/releases/<manifest-sha256>/   one verified tree per release
    apps/<app_id>/current -> releases/<manifest-sha256>
~/.local/share/applications/<app_id>.desktop -> ../asteroid-deploy/apps/<app_id>/current/launcher.desktop

The current release stays byte-for-byte unchanged until the new one is fully
extracted, read back and verified; the switch is one rename of the ``current``
symlink. If the launcher does not restart, ``current`` is switched back.
Bundle text is never executed or passed to a shell.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_bundle
from app_bundle import Bundle, BundleError

RESTART_LAUNCHER = ("/usr/bin/systemctl", "--user", "restart", "asteroid-launcher.service")
QML_RUN = "#!/bin/sh\nexport QT_QPA_PLATFORM=wayland\nexec /usr/bin/qml \"$1\"\n"

Restart = Callable[[], bool]


def restart_launcher() -> bool:
    try:
        return subprocess.run(RESTART_LAUNCHER, timeout=60, check=False, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def verify_tree(root: Path, bundle: Bundle, *, final_modes: bool) -> None:
    """The tree holds exactly the bundle's files, with their bytes (and modes)."""
    expected = {e.path: e for e in bundle.entries}
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            st = path.lstat()
            if stat.S_ISDIR(st.st_mode):
                if not any(p.startswith(rel + "/") for p in expected):
                    raise BundleError(f"unexpected directory {rel}")
                continue
            if not stat.S_ISREG(st.st_mode) or rel not in expected:
                raise BundleError(f"unexpected entry {rel}")
            entry = expected[rel]
            if path.read_bytes() != bundle.content(entry):
                raise BundleError(f"{rel}: content differs after extraction")
            if final_modes and stat.S_IMODE(st.st_mode) != entry.mode:
                raise BundleError(f"{rel}: mode {stat.S_IMODE(st.st_mode):o}, expected {entry.mode:o}")
            seen.add(rel)
    if seen != set(expected):
        raise BundleError(f"missing after extraction: {sorted(set(expected) - seen)}")


def check_desktop(bundle: Bundle) -> None:
    text = bundle.content(next(e for e in bundle.entries if e.path == app_bundle.DESKTOP))
    try:
        lines = [line.strip() for line in text.decode("utf-8", "strict").splitlines()]
    except UnicodeDecodeError:
        raise BundleError("launcher.desktop is not UTF-8") from None
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines or lines[0] != "[Desktop Entry]" or "Type=Application" not in lines:
        raise BundleError("launcher.desktop must be a [Desktop Entry] with Type=Application")
    execs = [line[5:] for line in lines if line.startswith("Exec=")]
    if len(execs) != 1:
        raise BundleError("launcher.desktop needs exactly one Exec=")
    marker = f"/asteroid-deploy/apps/{bundle.app_id}/current/"
    paths = {e.path for e in bundle.entries}
    for word in execs[0].split():
        if marker in word and word.split(marker, 1)[1] not in paths:
            raise BundleError(f"Exec references {word}, which the bundle does not contain")


def extract(bundle: Bundle, releases: Path) -> Path:
    release = releases / bundle.manifest_sha256
    if release.is_dir() and not release.is_symlink():
        verify_tree(release, bundle, final_modes=True)  # reuse only an identical release
        return release
    tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=releases))  # mode 0700
    try:
        for entry in bundle.entries:
            target = tmp / entry.path
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            write_new(target, bundle.content(entry))
        verify_tree(tmp, bundle, final_modes=False)
        for entry in bundle.entries:
            os.chmod(tmp / entry.path, entry.mode)
        verify_tree(tmp, bundle, final_modes=True)
        for dirpath, _, _ in os.walk(tmp, topdown=False):
            fsync_dir(Path(dirpath))
        os.chmod(tmp, 0o755)
        os.rename(tmp, release)
        fsync_dir(releases)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return release


def replace_symlink(link: Path, target: str) -> None:
    tmp = link.with_name(f".{link.name}.new-{os.getpid()}")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.rename(tmp, link)
    fsync_dir(link.parent)


def install(bundle_path: Path, home: Path, restart: Restart = restart_launcher) -> dict:
    bundle = app_bundle.parse(bundle_path.read_bytes())
    check_desktop(bundle)
    root = home / ".local/share/asteroid-deploy"
    app = root / "apps" / bundle.app_id
    releases = app / "releases"
    releases.mkdir(mode=0o755, parents=True, exist_ok=True)
    release = extract(bundle, releases)

    current = app / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    applications = home / ".local/share/applications"
    applications.mkdir(mode=0o755, parents=True, exist_ok=True)
    bridge = applications / f"{bundle.app_id}.desktop"
    bridge_target = os.path.relpath(current / app_bundle.DESKTOP, applications)
    old_bridge = bridge.read_bytes() if bridge.is_file() and not bridge.is_symlink() else None

    qml_run = home / ".local/bin/qml-run"
    if not qml_run.exists():
        qml_run.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        app_bundle.write_atomic(qml_run, QML_RUN.encode(), 0o755)

    replace_symlink(current, f"releases/{release.name}")  # the atomic activation point
    if not (bridge.is_symlink() and os.readlink(bridge) == bridge_target):
        replace_symlink(bridge, bridge_target)
    if not restart():
        if previous is not None:
            replace_symlink(current, previous)
        else:
            current.unlink()
            bridge.unlink()
            if old_bridge is not None:
                app_bundle.write_atomic(bridge, old_bridge)
        restart()
        raise RuntimeError("launcher did not restart; the previous release is active again")

    keep = {release.name, Path(previous).name if previous else None}
    for old in releases.iterdir():
        if old.name not in keep and not old.name.startswith("."):
            shutil.rmtree(old, ignore_errors=True)
    return {"status": "installed", "app_id": bundle.app_id, "release": release.name,
            "previous": Path(previous).name if previous else None}


def process_incoming(home: Path, restart: Restart = restart_launcher) -> list[dict]:
    incoming = home / ".local/share/asteroid-deploy/incoming"
    results = []
    for bundle_path in sorted(incoming.glob("*.aab")):
        try:
            result = install(bundle_path, home, restart)
        except Exception as error:  # noqa: BLE001 -- every failure becomes a .result for the client
            result = {"status": "error", "error": f"{type(error).__name__}: {error}"}
        finally:
            bundle_path.unlink(missing_ok=True)  # never retrigger the path unit on the same file
        result["bundle"] = bundle_path.name
        app_bundle.write_atomic(bundle_path.with_suffix(".result"), (json.dumps(result) + "\n").encode())
        results.append(result)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("bundle", nargs="?", type=Path, help="install this .aab")
    parser.add_argument("--incoming", action="store_true", help="install every incoming/*.aab")
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    if args.incoming == (args.bundle is not None):
        parser.error("give a bundle or --incoming")
    if args.incoming:
        results = process_incoming(args.home)
        for result in results:
            print(json.dumps(result))
        return 1 if any(r["status"] != "installed" for r in results) else 0
    try:
        print(json.dumps(install(args.bundle, args.home)))
    except (BundleError, OSError, RuntimeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
