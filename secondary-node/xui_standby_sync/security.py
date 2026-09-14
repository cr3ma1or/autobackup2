"""Filesystem security and safe cleanup primitives."""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from .commands import CommandError, run_command
from .constants import DIRECTORY_MODE, FILE_MODE, SAFE_WORK_ROOT
from .exceptions import SecurityViolationError


def verify_file_security(
    path: Path,
    label: str,
    *,
    required: bool = True,
    mode: int = FILE_MODE,
    allow_user_owned: bool = False,
) -> None:
    """Require a root-owned regular file without group/world access.

    When *allow_user_owned* is ``True``, non-root ownership is accepted
    provided the file is a regular, non-symlink file with strict
    permissions (no group/world bits).  This is used for the shared
    ``/opt/xui-backups`` tree where the unprivileged receiver user
    legitimately owns the incoming archives.
    """
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if required:
            raise SecurityViolationError(
                f"Required {label} is missing: {path}"
            ) from None
        return
    except OSError as error:
        raise SecurityViolationError(
            f"Cannot stat {label} ({path}): {error}"
        ) from error

    if path.is_symlink() or not stat.S_ISREG(path_stat.st_mode):
        raise SecurityViolationError(
            f"{label} must be a regular non-symlink file: {path}"
        )
    if path_stat.st_uid != 0 and not allow_user_owned:
        raise SecurityViolationError(f"{label} must be owned by root: {path}")
    if path_stat.st_mode & 0o077:
        raise SecurityViolationError(f"{label} has unsafe permissions: {path}")
    if mode and path_stat.st_mode & 0o777 != mode:
        raise SecurityViolationError(f"{label} must have mode {oct(mode)}: {path}")


def verify_directory_security(
    path: Path,
    label: str,
    *,
    mode: int = DIRECTORY_MODE,
    allow_user_owned: bool = False,
) -> None:
    """Require a root-owned non-symlink directory with strict permissions.

    When *allow_user_owned* is ``True``, non-root ownership is accepted
    provided the directory is a non-symlink directory with no group/world
    write bits.
    """
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise SecurityViolationError(
            f"Cannot stat {label} ({path}): {error}"
        ) from error
    if path.is_symlink() or not stat.S_ISDIR(path_stat.st_mode):
        raise SecurityViolationError(f"{label} must be a directory: {path}")
    if path_stat.st_uid != 0 and not allow_user_owned:
        raise SecurityViolationError(f"{label} must be owned by root: {path}")
    if path_stat.st_mode & 0o077:
        raise SecurityViolationError(f"{label} has unsafe permissions: {path}")
    if mode and path_stat.st_mode & 0o777 != mode:
        raise SecurityViolationError(f"{label} must have mode {oct(mode)}: {path}")


def verify_directory_chain(
    path: Path, label: str, *, allow_user_owned: bool = False
) -> None:
    """Reject symlinked, non-directory, or group/world-writable ancestors.

    When *allow_user_owned* is ``True``, directories owned by a
    non-root user are accepted provided they are not group/world-writable.
    """
    current = path.absolute()
    while True:
        try:
            path_stat = current.lstat()
        except OSError as error:
            raise SecurityViolationError(
                f"Cannot stat parent of {label} ({current}): {error}"
            ) from error
        if not stat.S_ISDIR(path_stat.st_mode) or (
            path_stat.st_uid != 0 and not allow_user_owned
        ):
            raise SecurityViolationError(
                f"Unsafe parent directory for {label}: {current}"
            )
        if path_stat.st_mode & 0o022:
            raise SecurityViolationError(
                f"Writable parent directory for {label}: {current}"
            )
        if current == current.parent:
            return
        current = current.parent


def validate_path_inside(path: Path, root: Path, label: str) -> Path:
    """Resolve a path and require it to be inside root."""
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise SecurityViolationError(
            f"{label} is outside protected root {resolved_root}: {path}"
        ) from error
    return resolved_path


def validate_backup_file(
    path: Path,
    label: str = "backup file",
    *,
    allow_user_owned: bool = True,
) -> None:
    verify_directory_chain(path.parent, label, allow_user_owned=allow_user_owned)
    verify_file_security(path, label, allow_user_owned=allow_user_owned)


def best_effort_wipe_file(path: Path, *, timeout: int = 15) -> None:
    """Best-effort wipe; does not claim physical deletion on journaled storage."""
    if not path.exists() or path.is_symlink() or not path.is_file():
        return
    try:
        result = run_command(
            ["shred", "-u", "-z", "-n", "1", str(path)],
            timeout=timeout,
            check=False,
        )
        if result.returncode == 0:
            return
    except (CommandError, OSError):
        pass

    try:
        with path.open("r+b", buffering=0) as handle:
            size = os.fstat(handle.fileno()).st_size
            handle.seek(0)
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
        path.unlink(missing_ok=True)
    except OSError:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def safe_clean_work_dir(work_dir: Path, *, safe_root: Path = SAFE_WORK_ROOT) -> None:
    """Remove only a strict descendant of the protected work root."""
    resolved = work_dir.resolve()
    if resolved in {
        Path("/"),
        Path("/opt"),
        Path("/etc"),
        Path("/var"),
        Path("/run"),
        safe_root.resolve(),
    }:
        raise SecurityViolationError(f"Refusing to remove critical path: {resolved}")
    resolved = validate_path_inside(work_dir, safe_root, "work directory")
    if work_dir.is_symlink():
        raise SecurityViolationError(
            f"Work directory must not be a symlink: {work_dir}"
        )
    if work_dir.exists():
        import shutil

        shutil.rmtree(work_dir)
