"""Read-only operational status inspection."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .commands import CommandError, run_command
from .constants import SERVICE_NAME
from .database import verify_integrity
from .exceptions import SyncError
from .models import PathsConfig
from .security import verify_file_security


def _lock_busy(path: Path) -> bool | None:
    """Inspect Linux lock state without creating or acquiring a lock."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        path_stat = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        with Path("/proc/locks").open(encoding="ascii") as locks:
            for line in locks:
                fields = line.split()
                if len(fields) < 6 or fields[1] != "FLOCK":
                    continue
                device_inode = fields[5].split(":")
                if len(device_inode) != 3:
                    continue
                major = int(device_inode[0], 16)
                minor = int(device_inode[1], 16)
                inode = int(device_inode[2], 10)
                if (
                    os.makedev(major, minor) == path_stat.st_dev
                    and inode == path_stat.st_ino
                ):
                    return True
        return False
    except (OSError, UnicodeError, ValueError):
        return None


def collect_status(
    paths: PathsConfig, *, service_name: str = SERVICE_NAME
) -> dict[str, Any]:
    mode = "UNKNOWN"
    marker_secure = False
    if paths.standby_mode_file.is_file() and not paths.standby_mode_file.is_symlink():
        try:
            verify_file_security(paths.standby_mode_file, "standby marker")
            mode = paths.standby_mode_file.read_text(encoding="utf-8").strip()
            marker_secure = True
        except (OSError, UnicodeDecodeError):
            marker_secure = False

    try:
        verify_integrity(paths.target_db)
        target_integrity = "ok"
    except (sqlite3.Error, OSError, SyncError):
        target_integrity = "failed"

    try:
        service_result = run_command(
            ["systemctl", "is-active", "--quiet", service_name],
            timeout=5,
            check=False,
        )
        service_active: bool | str = service_result.returncode == 0
    except CommandError:
        service_active = "unknown"
    return {
        "standby_mode": mode,
        "standby_marker_secure": marker_secure,
        "failover_lock_present": paths.failover_lock_file.exists(),
        "sync_lock_busy": _lock_busy(paths.sync_lock_path),
        "store_lock_busy": _lock_busy(paths.store_lock_path),
        "target_db_integrity": target_integrity,
        "xui_service_active": service_active,
    }


def status_json(paths: PathsConfig, *, service_name: str = SERVICE_NAME) -> str:
    return json.dumps(
        collect_status(paths, service_name=service_name),
        ensure_ascii=True,
        indent=2,
    )