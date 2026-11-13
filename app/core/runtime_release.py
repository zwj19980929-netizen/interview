"""Secret-free startup source identity, independent of interview availability.

This is a snapshot of the source tree at bootstrap, not an attestation of every
module loaded later. Releases must keep that tree unchanged during startup.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


_SCHEMA = "runtime-release.v1"
_ROLES = frozenset({"api", "worker", "worker_child", "manifest"})


@dataclass(frozen=True)
class _SourceSnapshot:
    status: str
    sha256: str | None
    file_count: int

    def public(self) -> dict:
        return {"status": self.status, "sha256": self.sha256,
                "file_count": self.file_count}


@dataclass(frozen=True)
class _ProcessIdentity:
    instance_id: str
    pid: int
    started_at: str


def _new_process_identity() -> _ProcessIdentity:
    return _ProcessIdentity(uuid4().hex, os.getpid(),
                            datetime.now(timezone.utc).isoformat())


def _open_directory(name, *, parent=None) -> int:
    # A descriptor for every ancestor prevents a directory-symlink swap from
    # redirecting a source read outside the selected root.
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                   dir_fd=parent)


def _file_digest(parent: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("source is not a regular file")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 128 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise OSError("source changed during capture")
        return digest.digest()
    finally:
        os.close(descriptor)


def _python_sources(directory: int, prefix: str):
    with os.scandir(directory) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for entry in children:
        if entry.name in {"__pycache__", "node_modules"} or (
            prefix == "app" and entry.name == "web"
        ):
            continue
        if entry.is_symlink():
            raise OSError("linked source entries are not supported")
        relative = prefix + "/" + entry.name
        if entry.is_dir(follow_symlinks=False):
            child = _open_directory(entry.name, parent=directory)
            try:
                yield from _python_sources(child, relative)
            finally:
                os.close(child)
        elif entry.name.endswith(".py"):
            # Other special files fail closed; non-Python contents are never
            # read. Directory and file opens both refuse symlink swaps.
            yield relative, _file_digest(directory, entry.name)


def _capture_source(root: Path | None = None) -> _SourceSnapshot:
    try:
        selected_root = root if root is not None else Path(__file__).absolute().parents[2]
        directory = _open_directory(selected_root)
        try:
            application = _open_directory("app", parent=directory)
            try:
                files = list(_python_sources(application, "app"))
            finally:
                os.close(application)
            files.append(("main.py", _file_digest(directory, "main.py")))
        finally:
            os.close(directory)
        digest = hashlib.sha256(b"interviewer-python-source.v1\0")
        for relative, content_digest in sorted(files):
            encoded = relative.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(content_digest)
        return _SourceSnapshot("available", digest.hexdigest(), len(files))
    except Exception:
        # Inspection cannot stop an interview or disclose an exception path.
        return _SourceSnapshot("unavailable", None, 0)


def source_manifest(root: Path | None = None) -> dict:
    """Capture a fresh source summary for an offline release comparison only."""
    return _capture_source(root).public()


class RuntimeRelease:
    """Freeze once; API/worker queries only copy bounded in-memory metadata."""

    def __init__(self, root: Path | None = None) -> None:
        self._source = _capture_source(root)
        self._process = _new_process_identity()
        self._bootstrap_pid = self._process.pid

    def snapshot(self, role: str) -> dict:
        if role not in _ROLES:
            raise ValueError("unsupported runtime role")
        if self._process.pid != os.getpid():
            # Prefork inherits the original source identity. It must never
            # re-read updated source and misrepresent the parent's loaded code.
            self._process = _new_process_identity()
        actual_role = (
            "worker_child" if role == "worker" and self._process.pid != self._bootstrap_pid
            else role
        )
        return {
            "schema": _SCHEMA,
            "process": {"instance_id": self._process.instance_id,
                        "pid": self._process.pid,
                        "started_at": self._process.started_at,
                        "role": actual_role},
            "source": self._source.public(),
        }


# Import this module before application/worker dependencies, not on first HTTP
# request. Files changed after bootstrap cannot replace this frozen identity.
_RUNTIME_RELEASE = RuntimeRelease()


def runtime_release_snapshot(role: str) -> dict:
    return _RUNTIME_RELEASE.snapshot(role)
