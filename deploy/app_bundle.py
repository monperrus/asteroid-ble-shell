"""asteroid-app-bundle-v1: pack and strictly parse an AsteroidOS app bundle (.aab).

Standard library only: the same module runs on the host (packing) and on the
watch (verifying before install). The format is specified in the watch repo's
dfu-deployment-design.md ("Inner application.bin"):

    offset  size  field
    0       8     magic "ASTDFU1\\0"
    8       2     container_version = 1 (LE)
    10      2     flags = 0
    12      4     manifest_length (LE)
    16      32    SHA-256 of the manifest bytes
    48      M     canonical UTF-8 JSON manifest (sorted keys, no spaces, final newline)
    48+M    ...   file payloads in manifest order, no trailing bytes
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"ASTDFU1\0"
CONTAINER_VERSION = 1
FORMAT = "asteroid-app-bundle-v1"
HEADER = struct.Struct("<8sHHI32s")  # 48 bytes
MAX_MANIFEST = 64 * 1024
MAX_IMAGE = 64 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
MAX_FILES = 1024
MODES = (0o644, 0o755)
DESKTOP = "launcher.desktop"
APP_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?")
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
FILE_KEYS = {"mode", "offset", "path", "sha256", "size"}


class BundleError(ValueError):
    """The bundle or app directory violates asteroid-app-bundle-v1."""


@dataclass(frozen=True)
class Entry:
    path: str
    mode: int
    offset: int
    size: int
    sha256: str


@dataclass(frozen=True)
class Bundle:
    app_id: str
    entries: tuple[Entry, ...]
    manifest_sha256: str
    data: bytes
    payload_start: int

    def content(self, entry: Entry) -> bytes:
        start = self.payload_start + entry.offset
        return self.data[start : start + entry.size]


def canonical(manifest: dict) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def check_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise BundleError(f"invalid path {path!r}")
    encoded = path.encode("utf-8", "strict")
    parts = path.split("/")
    if len(encoded) > 255 or len(parts) > 16:
        raise BundleError(f"path too long: {path!r}")
    if any(part in ("", ".", "..") for part in parts):
        raise BundleError(f"path has an empty, '.' or '..' component: {path!r}")
    if any(ord(c) < 32 or ord(c) == 127 or c == "\\" for c in path):
        raise BundleError(f"path has a control character or backslash: {path!r}")
    if unicodedata.normalize("NFC", path) != path:
        raise BundleError(f"path is not NFC-normalized: {path!r}")
    return path


def pack(app_id: str, files: dict[str, tuple[bytes, int]]) -> bytes:
    """files maps relative path -> (content, mode). Deterministic output."""
    entries, payload, offset = [], [], 0
    for path in sorted(files, key=lambda p: p.encode()):
        content, mode = files[path]
        entries.append({"mode": mode, "offset": offset, "path": path,
                        "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
        payload.append(content)
        offset += len(content)
    manifest = canonical({"app_id": app_id, "files": entries, "format": FORMAT})
    data = HEADER.pack(MAGIC, CONTAINER_VERSION, 0, len(manifest), hashlib.sha256(manifest).digest())
    data += manifest + b"".join(payload)
    parse(data)  # never produce what the watch would refuse
    return data


def parse(data: bytes) -> Bundle:
    """Verify everything the format requires, including every file hash."""
    if len(data) < HEADER.size:
        raise BundleError("shorter than the header")
    if len(data) > MAX_IMAGE:
        raise BundleError(f"larger than {MAX_IMAGE} bytes")
    magic, version, flags, manifest_len, manifest_hash = HEADER.unpack_from(data)
    if magic != MAGIC or version != CONTAINER_VERSION or flags != 0:
        raise BundleError("not an asteroid-app-bundle-v1 container")
    if manifest_len > MAX_MANIFEST or HEADER.size + manifest_len > len(data):
        raise BundleError("manifest length out of range")
    raw = data[HEADER.size : HEADER.size + manifest_len]
    if hashlib.sha256(raw).digest() != manifest_hash:
        raise BundleError("manifest SHA-256 mismatch")
    try:
        manifest = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BundleError(f"manifest is not UTF-8 JSON: {error}") from None
    if not isinstance(manifest, dict) or canonical(manifest) != raw:
        raise BundleError("manifest is not canonical JSON")
    if set(manifest) != {"app_id", "files", "format"} or manifest["format"] != FORMAT:
        raise BundleError("manifest fields or format are wrong")
    app_id = manifest["app_id"]
    if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id):
        raise BundleError(f"invalid app_id {app_id!r}")
    files = manifest["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise BundleError("files must list 1 to 1024 entries")

    entries: list[Entry] = []
    expected_offset = 0
    previous = b""
    for item in files:
        if not isinstance(item, dict) or set(item) != FILE_KEYS:
            raise BundleError("file entry fields are wrong")
        path = check_path(item["path"])
        ints = [item[k] for k in ("mode", "offset", "size")]
        if any(type(v) is not int for v in ints):
            raise BundleError(f"{path}: mode, offset and size must be integers")
        if item["mode"] not in MODES:
            raise BundleError(f"{path}: mode must be 0644 or 0755")
        if not 0 <= item["size"] <= MAX_FILE:
            raise BundleError(f"{path}: size out of range")
        if item["offset"] != expected_offset:
            raise BundleError(f"{path}: payloads are not contiguous")
        if not isinstance(item["sha256"], str) or not SHA256_HEX.fullmatch(item["sha256"]):
            raise BundleError(f"{path}: sha256 must be 64 lowercase hex digits")
        key = path.encode()
        if key <= previous:
            raise BundleError(f"{path}: files are not sorted or not unique")
        previous = key
        expected_offset += item["size"]
        entries.append(Entry(path, item["mode"], item["offset"], item["size"], item["sha256"]))

    paths = {e.path for e in entries}
    for e in entries:
        parts = e.path.split("/")
        for depth in range(1, len(parts)):
            if "/".join(parts[:depth]) in paths:
                raise BundleError(f"{e.path}: a parent directory is also a file")
    if DESKTOP not in paths:
        raise BundleError(f"{DESKTOP} is missing")

    payload_start = HEADER.size + manifest_len
    if len(data) != payload_start + expected_offset:
        raise BundleError("payload size does not match the manifest (truncated or trailing bytes)")
    bundle = Bundle(app_id, tuple(entries), hashlib.sha256(raw).hexdigest(), data, payload_start)
    for e in entries:
        if hashlib.sha256(bundle.content(e)).hexdigest() != e.sha256:
            raise BundleError(f"{e.path}: SHA-256 mismatch")
    return bundle


def desktop_entry(app_id: str, name: str, icon: str, main_qml: str) -> str:
    """The launcher entry for a QML-only app run by ~/.local/bin/qml-run."""
    release = f"/home/ceres/.local/share/asteroid-deploy/apps/{app_id}/current"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Categories=Applications;\n"
        f"Exec=/home/ceres/.local/bin/qml-run {release}/{main_qml}\n"
        f"Icon={icon}\n"
        "X-Asteroid-Center-Color=#314764\n"
        "X-Asteroid-Outer-Color=#07111E\n"
        f"Name={name}\n"
    )


def read_app(source: Path, *, app_id: str | None = None, name: str | None = None,
             icon: str = "ios-apps-outline", main: str | None = None) -> tuple[str, dict[str, tuple[bytes, int]]]:
    """Collect an app from a .qml file or a directory (hidden files skipped).

    A missing launcher.desktop is generated for the main QML file.
    """
    files: dict[str, tuple[bytes, int]] = {}
    if source.is_file():
        files[source.name] = (source.read_bytes(), 0o644)
        default_id = source.stem
    elif source.is_dir():
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source)
            if any(part.startswith(".") for part in rel.parts):
                continue
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise BundleError(f"{rel}: symlinks are not representable")
            if stat.S_ISREG(st.st_mode):
                files[rel.as_posix()] = (path.read_bytes(), 0o755 if st.st_mode & 0o111 else 0o644)
        default_id = source.name.removeprefix("app-")
    else:
        raise BundleError(f"{source}: no such file or directory")
    app_id = app_id or default_id.lower()
    if DESKTOP not in files:
        qmls = [p for p in files if p.endswith(".qml") and "/" not in p]
        main = main or ("main.qml" if "main.qml" in qmls else (qmls[0] if len(qmls) == 1 else None))
        if main is None or main not in files:
            raise BundleError("cannot tell the main QML file: pass --main")
        files[DESKTOP] = (desktop_entry(app_id, name or app_id, icon, main).encode(), 0o644)
    for path in files:
        check_path(path)
    return app_id, files


def write_atomic(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, mode)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, path)
