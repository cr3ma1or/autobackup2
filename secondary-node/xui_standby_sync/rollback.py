"""Immutable run-specific rollback snapshot lifecycle."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .constants import DIRECTORY_MODE, FILE_MODE
from .database import verify_integrity
from .exceptions import RollbackError
from .models import BackupMetadata, SnapshotMetadata
from .security import (
    verify_directory_chain,
    verify_directory_security,
    verify_file_security,
)

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f-]+$")


def _run_id() -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4()}"


def _prune_old_snapshots(snapshots_dir: Path, max_copies: int) -> None:
    if max_copies <= 0:
        return
    try:
        run_dirs = sorted(
            (
                p
                for p in snapshots_dir.iterdir()
                if p.is_dir() and _RUN_ID_RE.match(p.name)
            ),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    while len(run_dirs) > max_copies:
        oldest = run_dirs.pop(0)
        with contextlib.suppress(OSError):
            shutil.rmtree(oldest)


def create_rollback_snapshot(
    *,
    target_db: Path,
    snapshots_dir: Path,
    backup: BackupMetadata,
    max_rollback_copies: int = 5,
) -> SnapshotMetadata:
    run_id = _run_id()
    try:
        verify_directory_chain(snapshots_dir.parent, "snapshots directory")
        snapshots_dir.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
        verify_directory_security(snapshots_dir, "snapshots directory")
        run_dir = snapshots_dir / run_id
        run_dir.mkdir(mode=DIRECTORY_MODE)
        snapshot_path = run_dir / "target-before-sync.db"
        metadata_path = run_dir / "metadata.json"
        with (
            sqlite3.connect(target_db) as source,
            sqlite3.connect(snapshot_path) as target,
        ):
            source.backup(target)
        os.chmod(snapshot_path, FILE_MODE)
        verify_file_security(snapshot_path, "rollback snapshot")
        created_at = datetime.now(timezone.utc)
        metadata = SnapshotMetadata(
            run_id=run_id,
            snapshot_path=snapshot_path,
            created_at=created_at,
            archive_name=backup.archive_name,
            archive_sha256=backup.sha256_hash,
            signer_fingerprint=backup.signer_fingerprint,
        )
        metadata_path.write_text(
            json.dumps(
                {
                    "run_id": metadata.run_id,
                    "created_at": metadata.created_at.isoformat(),
                    "snapshot_path": str(metadata.snapshot_path),
                    "archive_name": metadata.archive_name,
                    "archive_sha256": metadata.archive_sha256,
                    "signer_fingerprint": metadata.signer_fingerprint,
                    "status": "created",
                },
                ensure_ascii=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.chmod(metadata_path, FILE_MODE)
        _prune_old_snapshots(snapshots_dir, max_rollback_copies)
        return metadata
    except (OSError, sqlite3.Error, RollbackError) as error:
        raise RollbackError(f"Cannot create rollback snapshot: {error}") from error


def restore_rollback_snapshot(*, target_db: Path, snapshot_path: Path) -> None:
    """Restore a verified snapshot after removing stale SQLite sidecars."""
    temporary: Path | None = None
    try:
        verify_file_security(snapshot_path, "rollback snapshot")
        verify_integrity(snapshot_path)

        if not hasattr(os, "O_NOFOLLOW"):
            raise RollbackError("O_NOFOLLOW is required for rollback files")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
        descriptor = -1
        while descriptor < 0:
            candidate = target_db.parent / (
                f".{target_db.name}.rollback-{uuid.uuid4()}.tmp"
            )
            try:
                descriptor = os.open(candidate, flags, FILE_MODE)
                temporary = candidate
            except FileExistsError:
                continue
        try:
            with snapshot_path.open("rb") as source, os.fdopen(
                descriptor, "wb"
            ) as destination:
                descriptor = -1
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if temporary is None:
            raise RollbackError("Temporary rollback file was not created")
        verify_file_security(temporary, "temporary rollback file")
        verify_integrity(temporary)

        for suffix in ("-wal", "-shm"):
            side_file = Path(f"{target_db}{suffix}")
            with contextlib.suppress(FileNotFoundError):
                side_file.unlink()

        os.replace(temporary, target_db)
        temporary = None
        os.chmod(target_db, FILE_MODE)
        verify_integrity(target_db)
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(error, RollbackError):
            raise
        raise RollbackError(f"Cannot restore rollback snapshot: {error}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
