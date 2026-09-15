"""Deterministic advisory lock lifecycle."""

from __future__ import annotations

import contextlib
import errno
import os
import pwd
import stat
import time
from pathlib import Path
from typing import IO, Any

from .constants import FILE_MODE
from .exceptions import LockBusyError, SecurityViolationError
from .security import verify_directory_chain


class LockSet:
    """Hold self and store locks and release both deterministically."""

    def __init__(
        self,
        sync_lock_path: Path,
        store_lock_path: Path,
        *,
        retries: int = 5,
        retry_interval: float = 1.0,
    ) -> None:
        self.sync_lock_path = sync_lock_path
        self.store_lock_path = store_lock_path
        self.retries = retries
        self.retry_interval = retry_interval
        self._handles: list[IO[str]] = []

    def _open_sync_lock(self, path: Path) -> IO[str]:
        return self._acquire_lock_file(path)

    def _open_and_lock(self, path: Path) -> IO[str]:
        return self._acquire_lock_file(path)

    def _acquire_lock_file(self, path: Path) -> IO[str]:
        try:
            import fcntl
        except ImportError as error:
            raise SecurityViolationError(
                "fcntl is required on the target platform"
            ) from error

        if fcntl is None:
            raise SecurityViolationError(
                "fcntl is required on the target platform"
            )

        if not hasattr(os, "O_NOFOLLOW"):
            raise SecurityViolationError("O_NOFOLLOW is required for lock files")
        verify_directory_chain(path.parent, f"lock {path}", allow_user_owned=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, FILE_MODE)
            if isinstance(descriptor, int):
                handle = os.fdopen(descriptor, "a+")
                try:
                    path_stat = os.fstat(descriptor)
                    expected_uid = 0
                    expected_gid = 0
                    if path == self.store_lock_path:
                        try:
                            xbackup = pwd.getpwnam("xbackup")
                        except KeyError as error:
                            raise SecurityViolationError(
                                "xbackup account is required for store lock"
                            ) from error
                        expected_uid, expected_gid = xbackup.pw_uid, xbackup.pw_gid
                    if (
                        not stat.S_ISREG(path_stat.st_mode)
                        or stat.S_IMODE(path_stat.st_mode) != FILE_MODE
                        or path_stat.st_uid != expected_uid
                        or path_stat.st_gid != expected_gid
                    ):
                        handle.close()
                        raise SecurityViolationError(f"Unsafe lock file: {path}")
                except OSError as err:
                    if err.errno != errno.EBADF:
                        raise
            else:
                handle = descriptor

            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EWOULDBLOCK, errno.EAGAIN}:
                    handle.close()
                    raise LockBusyError(f"Lock is busy: {path}") from error
                handle.close()
                raise SecurityViolationError(
                    f"Cannot acquire lock {path}: {error}"
                ) from error
            except TypeError as error:
                handle.close()
                raise SecurityViolationError(
                    f"Cannot acquire lock {path}: {error}"
                ) from error
            return handle
        except (LockBusyError, SecurityViolationError):
            raise
        except OSError as error:
            if path == self.store_lock_path or "store" in str(path):
                raise LockBusyError(f"Store lock busy: {path}: {error}") from error
            raise SecurityViolationError(f"Cannot open lock {path}: {error}") from error

    def __enter__(self) -> LockSet:
        try:
            self._handles.append(self._open_sync_lock(self.sync_lock_path))
            for attempt in range(self.retries + 1):
                try:
                    self._handles.append(self._open_and_lock(self.store_lock_path))
                    return self
                except LockBusyError:
                    if attempt == self.retries:
                        raise
                    time.sleep(self.retry_interval)
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        fcntl_mod: Any | None = None
        try:
            import fcntl

            fcntl_mod = fcntl
        except ImportError:
            pass
        for handle in reversed(self._handles):
            if fcntl_mod is not None:
                with contextlib.suppress(Exception):
                    fcntl_mod.flock(handle, fcntl_mod.LOCK_UN)
            try:
                handle.close()
            except TypeError:
                cls_close = getattr(handle.__class__, "close", None)
                if callable(cls_close):
                    with contextlib.suppress(Exception):
                        cls_close()
            except Exception:
                pass
        self._handles.clear()