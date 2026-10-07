"""Private, atomic local state and cross-process locking."""

import json
import os
import tempfile
from pathlib import Path

import portalocker

from .errors import WorkspaceError


def private_directory(path: Path) -> None:
    if path.exists():
        return
    # Path.mkdir(parents=True) applies mode only to the leaf, not its new parents.
    private_directory(path.parent)
    path.mkdir(exist_ok=True, mode=0o700)


def atomic_write(path: Path, text: str) -> None:
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, data: dict) -> None:
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise WorkspaceError(f"无法读取状态文件：{path.name}") from None


def lock(path: Path):
    private_directory(path.parent)
    # Never unlink lock files: doing so could allow two processes to lock different inodes.
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    return portalocker.Lock(str(path), mode="a", timeout=0)


def is_locked(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with lock(path):
            return False
    except portalocker.exceptions.LockException:
        return True
