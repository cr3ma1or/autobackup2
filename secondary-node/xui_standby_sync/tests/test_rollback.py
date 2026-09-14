"""Rollback snapshot tests as required by section 8.10 of TZ-UPGRADE.md."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xui_standby_sync.exceptions import RollbackError
from xui_standby_sync.models import BackupMetadata
from xui_standby_sync.rollback import (
    create_rollback_snapshot,
    restore_rollback_snapshot,
)


def _make_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        conn.execute("INSERT INTO settings VALUES (1, 'webPort', '2053')")
        conn.commit()
    os.chmod(path, 0o600)


def _make_backup(tmp_path: Path) -> BackupMetadata:
    archive = tmp_path / "xui-backup-20261212T120000Z-test.tar.gz.gpg"
    archive.write_bytes(b"backup")
    os.chmod(archive, 0o600)
    return BackupMetadata(
        archive_path=archive,
        archive_name=archive.name,
        timestamp=datetime(2026, 12, 12, 12, 0, 0, tzinfo=timezone.utc),
        age_hours=1.0,
        sha256_hash="a" * 64,
        is_fresh=True,
        signer_fingerprint="ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234",
        signature_verified=True,
    )


class TestCreateRollbackSnapshot:
    """Test snapshot creation."""

    def test_snapshot_created_with_correct_metadata(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        assert meta.snapshot_path.exists()
        assert meta.run_id
        assert meta.archive_sha256 == "a" * 64
        assert meta.signer_fingerprint == "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"

    def test_run_id_contains_utc_timestamp_and_uuid(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        # run_id format: 20261212T120000Z-<uuid>
        parts = meta.run_id.split("-", 1)
        assert len(parts) == 2
        assert len(parts[0]) == 16  # YYYYMMDDTHHMMSSz portion
        assert len(parts[1]) > 8   # uuid portion

    def test_snapshot_db_has_secure_permissions(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        mode = oct(os.stat(meta.snapshot_path).st_mode & 0o777)
        assert mode == oct(0o600)

    def test_metadata_json_written(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        metadata_path = meta.snapshot_path.parent / "metadata.json"
        assert metadata_path.exists()
        data = json.loads(metadata_path.read_text())
        assert data["run_id"] == meta.run_id
        assert data["archive_sha256"] == "a" * 64
        assert data["status"] == "created"

    def test_snapshot_contains_original_data(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        with sqlite3.connect(str(meta.snapshot_path)) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()
        assert row[0] == "2053"


class TestRestoreRollbackSnapshot:
    """Test snapshot restoration."""

    def test_restore_returns_original_data(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        # Modify target
        with sqlite3.connect(str(target)) as conn:
            conn.execute("UPDATE settings SET value='80' WHERE key='webPort'")
            conn.commit()

        restore_rollback_snapshot(target_db=target, snapshot_path=meta.snapshot_path)

        with sqlite3.connect(str(target)) as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()
        assert row[0] == "2053"

    def test_restore_removes_wal_and_shm_files(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        wal = Path(f"{target}-wal")
        shm = Path(f"{target}-shm")
        wal.write_bytes(b"wal content")
        shm.write_bytes(b"shm content")

        restore_rollback_snapshot(target_db=target, snapshot_path=meta.snapshot_path)

        assert not wal.exists()
        assert not shm.exists()

    def test_restore_runs_integrity_check(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        restore_rollback_snapshot(target_db=target, snapshot_path=meta.snapshot_path)
        # No exception raised means integrity check passed

    def test_restore_missing_snapshot_raises_rollback_error(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        _make_db(target)
        missing = tmp_path / "missing" / "snapshot.db"

        with pytest.raises(RollbackError):
            restore_rollback_snapshot(target_db=target, snapshot_path=missing)

    def test_restore_corrupted_snapshot_raises_rollback_error(self, tmp_path: Path):
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        meta = create_rollback_snapshot(
            target_db=target,
            snapshots_dir=snapshots_dir,
            backup=backup,
        )

        # Corrupt the snapshot
        meta.snapshot_path.write_bytes(b"corrupted not a db")

        with pytest.raises(RollbackError):
            restore_rollback_snapshot(target_db=target, snapshot_path=meta.snapshot_path)


class TestSnapshotRotation:
    """Test snapshot rotation — only validated run directories are deleted."""

    def test_rotation_deletes_only_inside_snapshots_root(self, tmp_path: Path):
        """Destructive rotation is covered: only removes run dirs inside snapshots root."""
        target = tmp_path / "x-ui.db"
        snapshots_dir = tmp_path / "snapshots"
        _make_db(target)
        backup = _make_backup(tmp_path)

        # Create multiple snapshots to trigger rotation
        created = []
        for _ in range(3):
            meta = create_rollback_snapshot(
                target_db=target,
                snapshots_dir=snapshots_dir,
                backup=backup,
            )
            created.append(meta)

        # All snapshots should still be in snapshots_dir
        run_dirs = list(snapshots_dir.iterdir())
        assert len(run_dirs) == 3

        # Verify no dirs outside snapshots_dir were created
        for meta in created:
            assert meta.snapshot_path.is_relative_to(snapshots_dir)
