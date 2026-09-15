"""Password-protected 7z container v1. Optional backend imported on use.

Only ordinary files/directories; no extraction to user-controlled archive paths.
See docs/specs/container.md for compatibility rules and resource boundaries.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import getpass
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import warnings

from .protocol import TransferDescriptor, sha256_file

MANIFEST = "control/manifest.json"
AES = b"\x06\xf1\x07\x01"
PROFILES = {"strong": (6, 16 << 20), "max": (9, 32 << 20),
            "ultra": (9 | lzma.PRESET_EXTREME, 64 << 20)}


class ContainerError(ValueError):
    """Stable public code, never backend exception text or password."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ContainerLimits:
    object_bytes: int = 64 << 20
    expanded_bytes: int = 256 << 20
    entries: int = 10000
    manifest_bytes: int = 2 << 20
    disk_reserve: int = 16 << 20

    def __post_init__(self):
        if any(type(x) is not int or x <= 0 for x in self.__dict__.values()):
            raise ValueError("Invalid container limits")


def backend():
    try:
        import py7zr
    except ImportError:
        raise ContainerError("install_container_extra") from None
    if py7zr.__version__ != "1.1.3":
        raise ContainerError("unsupported_backend_version")
    return py7zr


def password_input(*, confirm: bool = False) -> str:
    # Fail rather than silently falling back to echoed input in a non-terminal.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Password: ")
            check_password(password)
            if confirm and password != getpass.getpass("Repeat password: "):
                raise ContainerError("password_mismatch")
            return password
    except (getpass.GetPassWarning, EOFError):
        raise ContainerError("hidden_password_input_unavailable") from None


def check_password(password):
    if not isinstance(password, str) or not password:
        raise ContainerError("empty_password")


def valid_path(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or len(name.encode("utf-8", errors="surrogatepass")) > 1024:
        raise ContainerError("unsafe_path")
    parts = name.split("/")
    if len(parts) > 64:
        raise ContainerError("path_depth")
    for part in parts:
        if (not part or part in (".", "..") or part[-1] in " ." or
                any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or c in '<>:"|?*' for c in part) or
                len(part.encode("utf-16-le", errors="surrogatepass")) // 2 > 255 or
                re.fullmatch(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|CLOCK\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part, re.I)):
            raise ContainerError("unsafe_path")
    return unicodedata.normalize("NFC", name).casefold()


def validate_names(entries: dict):
    canonical = {}
    for name in entries:
        key = valid_path(name)
        if key in canonical:
            raise ContainerError("name_collision")
        canonical[key] = name
    for name in entries:
        parts = name.split("/")
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            key = valid_path(parent)
            if key in canonical and canonical[key] != parent:
                raise ContainerError("name_collision")
            if parent in entries and entries[parent]["type"] != "directory":
                raise ContainerError("path_type_conflict")


def _plain(st):
    if getattr(st, "st_file_attributes", 0) & 0x400 or stat.S_ISLNK(st.st_mode):
        raise ContainerError("links_not_supported")
    if stat.S_ISREG(st.st_mode):
        if st.st_nlink != 1:
            raise ContainerError("hardlinks_not_supported")
        return "file"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    raise ContainerError("special_files_not_supported")


def plain_ancestors(path: Path):
    for part in (path, *path.parents):
        if _plain(part.lstat()) != "directory":
            raise ContainerError("unsafe_destination_parent")


def fingerprint(st):
    # Windows lstat/fstat can disagree on ctime (creation vs change time).
    # Identity, link count, size and mtime remain comparable across both calls.
    change_time = st.st_ctime_ns if os.name != "nt" else 0
    return (st.st_dev, st.st_ino, st.st_mode, st.st_nlink, st.st_size, st.st_mtime_ns, change_time)


def inventory(source: Path, limits: ContainerLimits):
    result = {}
    total = 0
    def visit(path, name):
        nonlocal total
        info = path.lstat()
        kind = _plain(info)
        valid_path(name)
        if len(result) >= limits.entries:
            raise ContainerError("entry_limit")
        result[name] = {"type": kind, "stat": fingerprint(info)}
        if kind == "file":
            total += info.st_size
            if total > limits.expanded_bytes:
                raise ContainerError("expanded_size_limit")
        else:
            for child in sorted(path.iterdir(), key=lambda p: p.name):
                visit(child, name + "/" + child.name)
    visit(source, source.name)
    validate_names(result)
    return result, total


def _space(parent: Path, required: int, limits: ContainerLimits):
    if shutil.disk_usage(parent).free < required + limits.disk_reserve:
        raise ContainerError("insufficient_disk")


def _cleanup(work: Path, parent: Path):
    # Only remove the private temporary directory created directly in this parent.
    if work.is_symlink() or work.resolve().parent != parent.resolve() or not work.name.startswith(".qr-transfer-"):
        raise ContainerError("unsafe_cleanup_path")
    shutil.rmtree(work)


def _publish_file(source: Path, destination: Path):
    # Atomic no-replace publication on the same filesystem, including POSIX.
    os.link(source, destination)
    source.unlink()


def _publish_directory(source: Path, destination: Path):
    if os.name == "nt":
        os.rename(source, destination)  # Windows refuses an existing destination.
    elif sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ContainerError("atomic_publish_unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            raise OSError(ctypes.get_errno(), "Cannot publish result")
    else:
        raise ContainerError("unsupported_platform")


def _require_encrypted(archive):
    folders = archive.header.main_streams.unpackinfo.folders
    if not folders or any(AES not in [coder["method"] for coder in folder.coders] for folder in folders):
        raise ContainerError("unencrypted_content")
    for folder in folders:
        for coder in folder.coders:
            if coder["method"] not in (AES, b"\x21"):
                raise ContainerError("unsupported_container_codec")
            # Constrain data LZMA2 dictionaries before creating the decompressor.
            if coder["method"] == b"\x21":
                prop = coder["properties"]
                if len(prop) != 1 or prop[0] > 28:  # LZMA2 property 28 = 64 MiB.
                    raise ContainerError("dictionary_limit")


def _header_hidden(path: Path):
    py7zr = backend()
    try:
        with py7zr.SevenZipFile(path, "r") as archive:
            archive.getnames()
    except py7zr.exceptions.PasswordRequired:
        return
    raise ContainerError("unencrypted_header")


def prepare(source: Path, destination: Path, password: str, *, profile="max",
            limits: ContainerLimits = ContainerLimits()) -> Path:
    check_password(password)
    py7zr = backend()
    if profile not in PROFILES:
        raise ContainerError("unknown_compression_profile")
    source, destination = source.absolute(), destination.absolute()
    plain_ancestors(source.parent)
    plain_ancestors(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ContainerError("destination_exists")
    if destination.parent.is_relative_to(source):
        raise ContainerError("output_inside_source")
    before, total = inventory(source, limits)
    _space(destination.parent, total + limits.object_bytes, limits)
    work = Path(tempfile.mkdtemp(prefix=".qr-transfer-", dir=destination.parent))
    try:
        snapshot = work / "snapshot"
        snapshot.mkdir()
        entries = []
        for name, item in before.items():
            target = snapshot / name
            if item["type"] == "directory":
                target.mkdir()
                entries.append({"path": name, "type": "directory"})
                continue
            original = source.parent / name
            # Parent traversal was inventoried; reject replaced links on each read.
            plain_ancestors(original.parent)
            fd = os.open(original, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(fd, "rb") as reader, target.open("xb") as writer:
                if fingerprint(os.fstat(reader.fileno())) != item["stat"]:
                    raise ContainerError("source_changed")
                for block in iter(lambda: reader.read(1 << 20), b""):
                    size += len(block)
                    if size > item["stat"][4]:
                        raise ContainerError("source_changed")
                    digest.update(block)
                    writer.write(block)
                if fingerprint(os.fstat(reader.fileno())) != item["stat"] or size != item["stat"][4]:
                    raise ContainerError("source_changed")
            entries.append({"path": name, "type": "file", "size": size, "sha256": digest.hexdigest()})
        manifest = json.dumps({"schema": 1, "root": source.name, "entries": entries}, ensure_ascii=True,
                              separators=(",", ":")).encode("ascii")
        if len(manifest) > limits.manifest_bytes:
            raise ContainerError("manifest_limit")
        preset, dictionary = PROFILES[profile]
        filters = [{"id": py7zr.FILTER_LZMA2, "preset": preset, "dict_size": dictionary},
                   {"id": py7zr.FILTER_CRYPTO_AES256_SHA256}]
        staged = work / "object.7z"
        with py7zr.SevenZipFile(staged, "w", password=password, header_encryption=True, filters=filters) as archive:
            archive.writestr(manifest, MANIFEST)
            for entry in entries:
                archive.write(snapshot / entry["path"], "data/" + entry["path"])
        if staged.stat().st_size > limits.object_bytes:
            raise ContainerError("object_size_limit")
        if inventory(source, limits)[0] != before:
            raise ContainerError("source_changed")
        _header_hidden(staged)
        with py7zr.SevenZipFile(staged, "r", password=password) as archive:
            _require_encrypted(archive)
        _publish_file(staged, destination)
        return destination
    except (ContainerError, KeyboardInterrupt):
        raise
    except Exception:
        raise ContainerError("prepare_failed") from None
    finally:
        _cleanup(work, destination.parent)


def verify_object(path: Path, descriptor: TransferDescriptor, limits=ContainerLimits()):
    descriptor.validate()
    if descriptor.container != "7z-aes256":
        raise ContainerError("wrong_container_type")
    if not 0 < path.stat().st_size <= limits.object_bytes:
        raise ContainerError("object_size_limit")
    if path.stat().st_size != descriptor.object_size or sha256_file(path) != descriptor.sha256:
        raise ContainerError("transport_integrity")


def inspect_container(path: Path, descriptor: TransferDescriptor, limits=ContainerLimits()) -> dict:
    """Public technical inspection, no source names and no password needed."""
    verify_object(path, descriptor, limits)
    try:
        _header_hidden(path)
    except ContainerError:
        raise
    except Exception:
        raise ContainerError("damaged_container") from None
    return {"schema": 1, "object_size": descriptor.object_size, "sha256": descriptor.sha256,
            "content_verified": True, "header_encrypted": True,
            "payload_encryption_checked": False}


def _manifest(raw: bytes, limits):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContainerError("manifest_duplicate_key")
            result[key] = value
        return result
    obj = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(obj, dict) or set(obj) != {"schema", "root", "entries"} or type(obj["schema"]) is not int or obj["schema"] != 1:
        raise ContainerError("manifest_format")
    root, records = obj["root"], obj["entries"]
    valid_path(root)
    if "/" in root or not isinstance(records, list) or not 1 <= len(records) <= limits.entries:
        raise ContainerError("manifest_format")
    entries = {}
    total = 0
    for entry in records:
        if not isinstance(entry, dict):
            raise ContainerError("manifest_format")
        name, kind = entry.get("path"), entry.get("type")
        valid_path(name)
        expected = {"path", "type"} if kind == "directory" else {"path", "type", "size", "sha256"}
        if kind not in ("file", "directory") or set(entry) != expected or name in entries or not (name == root or name.startswith(root + "/")):
            raise ContainerError("manifest_format")
        if kind == "file":
            size, digest = entry["size"], entry["sha256"]
            if type(size) is not int or size < 0 or not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
                raise ContainerError("manifest_format")
            total += size
        entries[name] = entry
    if total > limits.expanded_bytes:
        raise ContainerError("expanded_size_limit")
    if root not in entries:
        raise ContainerError("manifest_missing_root")
    validate_names(entries)
    for name in entries:
        if name != root and "/".join(name.split("/")[:-1]) not in entries:
            raise ContainerError("manifest_missing_parent")
    return entries


def extract(archive_path: Path, destination: Path, password: str, *, descriptor: TransferDescriptor,
            limits: ContainerLimits = ContainerLimits()) -> Path:
    """Verify encrypted bytes first; publish a new directory containing original root."""
    check_password(password)
    py7zr = backend()
    archive_path, destination = archive_path.absolute(), destination.absolute()
    plain_ancestors(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ContainerError("destination_exists")
    verify_object(archive_path, descriptor, limits)
    work = Path(tempfile.mkdtemp(prefix=".qr-transfer-", dir=destination.parent))
    try:
        _header_hidden(archive_path)
        with py7zr.SevenZipFile(archive_path, "r", password=password, blocksize=1 << 20,
                               max_extract_size=limits.expanded_bytes + limits.manifest_bytes) as archive:
            _require_encrypted(archive)
            members = {}
            total = 0
            for member in archive.files:
                name = member.filename
                valid_path(name)
                if len(members) >= limits.entries + 1:
                    raise ContainerError("entry_limit")
                if name in members:
                    raise ContainerError("duplicate_member")
                if member.is_symlink or member.is_junction or member.is_socket or not (member.is_file or member.is_directory):
                    raise ContainerError("links_or_special_member")
                kind = "directory" if member.is_directory else "file"
                if name != MANIFEST and not name.startswith("data/"):
                    raise ContainerError("unexpected_member")
                size = member.uncompressed or 0
                if type(size) is not int or size < 0 or (kind == "directory" and size):
                    raise ContainerError("member_size")
                members[name] = {"type": kind, "size": size}
                total += size
            validate_names(members)
            if MANIFEST not in members or members[MANIFEST]["type"] != "file" or members[MANIFEST]["size"] > limits.manifest_bytes:
                raise ContainerError("manifest_limit")
            if total - members[MANIFEST]["size"] > limits.expanded_bytes:
                raise ContainerError("expanded_size_limit")
            _space(destination.parent, total, limits)
            # Factory never uses archive member names as filesystem paths.
            # Numeric spool files are private and bounded before every write.
            factory = _factory(py7zr, work, members, limits)
            try:
                archive.extractall(factory=factory)
            finally:
                factory.close()
        if set(factory.paths) != {name for name, info in members.items() if info["type"] == "file"}:
            raise ContainerError("missing_member")
        for name, path in factory.paths.items():
            if path.stat().st_size != members[name]["size"]:
                raise ContainerError("member_size")
        entries = _manifest(factory.paths[MANIFEST].read_bytes(), limits)
        expected = {"data/" + name for name in entries} | {MANIFEST}
        if set(members) != expected:
            raise ContainerError("manifest_tree_mismatch")
        for name, entry in entries.items():
            member_name = "data/" + name
            if entry["type"] != members[member_name]["type"]:
                raise ContainerError("manifest_type_mismatch")
            if entry["type"] == "file":
                if entry["size"] != members[member_name]["size"] or sha256_file(factory.paths[member_name]) != entry["sha256"]:
                    raise ContainerError("manifest_hash_mismatch")
        result = work / "result"
        result.mkdir()
        for name, entry in sorted(entries.items(), key=lambda pair: (pair[0].count("/"), pair[0])):
            target = result / name
            if entry["type"] == "directory":
                target.mkdir()
            else:
                factory.paths["data/" + name].rename(target)
        # Catch local replacement/modification during extraction as well.
        verify_object(archive_path, descriptor, limits)
        _publish_directory(result, destination)
        return destination
    except (ContainerError, KeyboardInterrupt):
        raise
    except Exception:
        raise ContainerError("password_or_damaged_container") from None
    finally:
        _cleanup(work, destination.parent)


def _factory(py7zr, work, members, limits):
    class Sink(py7zr.Py7zIO):
        def __init__(self, owner, name, path):
            self.owner, self.name = owner, name
            self.file = path.open("x+b")
            self.count = 0

        def write(self, data):
            if self.file.tell() != self.count:
                raise ContainerError("nonsequential_write")
            if self.count + len(data) > members[self.name]["size"] or self.owner.count + len(data) > limits.expanded_bytes + limits.manifest_bytes:
                raise ContainerError("actual_write_limit")
            _space(work, len(data), limits)
            count = self.file.write(data)
            self.count += count
            self.owner.count += count
            return count

        def read(self, size=None):
            return self.file.read(-1 if size is None else size)

        def seek(self, offset, whence=0):
            return self.file.seek(offset, whence)

        def flush(self):
            self.file.flush()

        def size(self):
            return self.count

        def close(self):
            self.file.close()

    class Factory(py7zr.WriterFactory):
        def __init__(self):
            self.paths, self.sinks, self.count = {}, [], 0

        def create(self, filename):
            if filename not in members or members[filename]["type"] != "file" or filename in self.paths:
                raise ContainerError("unexpected_output")
            path = work / f"spool-{len(self.paths):08x}"
            sink = Sink(self, filename, path)
            self.paths[filename] = path
            self.sinks.append(sink)
            return sink

        def close(self):
            for sink in self.sinks:
                sink.close()

    return Factory()
