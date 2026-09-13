"""Read-only operational status inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .commands import run_command
from .constants import SERVICE_NAME
from .database import verify_integrity
from .models import PathsConfig
from .security import verify_file_security


def _lock_busy(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        with path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[attr-defined]
            return False
    except BlockingIOError:
        return True
    except OSError:
        return True


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
        except Exception:
            marker_secure = False
    try:
        verify_integrity(paths.target_db)
        target_integrity = "ok"
    except Exception:
        target_integrity = "failed"
    service_result = run_command(
        ["systemctl", "is-active", "--quiet", service_name],
        timeout=5,
        check=False,
    )
    return {
        "standby_mode": mode,
        "standby_marker_secure": marker_secure,
        "failover_lock_present": paths.failover_lock_file.exists(),
        "sync_lock_busy": _lock_busy(paths.sync_lock_path),
        "store_lock_busy": _lock_busy(paths.store_lock_path),
        "target_db_integrity": target_integrity,
        "xui_service_active": service_result.returncode == 0,
    }


def status_json(paths: PathsConfig, *, service_name: str = SERVICE_NAME) -> str:
    return json.dumps(
        collect_status(paths, service_name=service_name),
        ensure_ascii=True,
        indent=2,
    )
