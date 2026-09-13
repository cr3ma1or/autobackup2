"""Deterministic advisory lock lifecycle."""

from __future__ import annotations

import contextlib
import os
import stat
import time
from pathlib import Path
from typing import IO

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

    def _open_and_lock(self, path: Path) -> IO[str]:
        try:
            import fcntl
        except ImportError as error:
            raise SecurityViolationError(
                "fcntl is required on the target platform"
            ) from error
        verify_directory_chain(path.parent, f"lock {path}", allow_user_owned=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, FILE_MODE)
            handle = os.fdopen(descriptor, "a+")
            path_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_mode & 0o077
            ):
                handle.close()
                raise SecurityViolationError(f"Unsafe lock file: {path}")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                handle.close()
                raise LockBusyError(f"Lock is busy: {path}") from error
            return handle
        except OSError as error:
            raise SecurityViolationError(f"Cannot open lock {path}: {error}") from error

    def __enter__(self) -> LockSet:
        try:
            self._handles.append(self._open_and_lock(self.sync_lock_path))
            for attempt in range(self.retries):
                try:
                    self._handles.append(self._open_and_lock(self.store_lock_path))
                    return self
                except LockBusyError:
                    if attempt + 1 == self.retries:
                        raise
                    time.sleep(self.retry_interval)
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            import fcntl
        except ImportError:
            fcntl = None
        for handle in reversed(self._handles):
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                handle.close()
        self._handles.clear()
