"""Status inspection tests."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xui_standby_sync.models import PathsConfig
from xui_standby_sync.status import collect_status, status_json


def _create_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.commit()
    os.chmod(path, 0o600)


class TestCollectStatus:
    """Test status collection."""

    def test_standby_mode_file_validation(self, tmp_path: Path):
        standby_file = tmp_path / "standby-mode"
        standby_file.write_text("STANDBY")
        os.chmod(standby_file, 0o600)

        paths = PathsConfig(
            target_db=tmp_path / "x-ui.db",
            allowlist_path=tmp_path / "allowlist.json",
            incoming_dir=tmp_path / "incoming",
            work_root=tmp_path / "work",
            snapshots_dir=tmp_path / "snapshots",
            gnupg_dir=tmp_path / "gnupg",
            sync_lock_path=tmp_path / "sync.lock",
            store_lock_path=tmp_path / "store.lock",
            standby_mode_file=standby_file,
            failover_lock_file=tmp_path / "failover.lock",
            log_file=tmp_path / "sync.log",
        )

        _create_db(paths.target_db)

        with patch("xui_standby_sync.status.run_command") as mock:
            mock.return_value = MagicMock(returncode=0)
            status = collect_status(paths)

        assert status["standby_mode"] == "STANDBY"
        assert status["standby_marker_secure"] is True

    def test_standby_mode_symlink_rejection(self, tmp_path: Path):
        real_file = tmp_path / "real-standby"
        real_file.write_text("STANDBY")
        standby_file = tmp_path / "standby-mode"
        standby_file.symlink_to(real_file)

        paths = PathsConfig(
            target_db=tmp_path / "x-ui.db",
            allowlist_path=tmp_path / "allowlist.json",
            incoming_dir=tmp_path / "incoming",
            work_root=tmp_path / "work",
            snapshots_dir=tmp_path / "snapshots",
            gnupg_dir=tmp_path / "gnupg",
            sync_lock_path=tmp_path / "sync.lock",
            store_lock_path=tmp_path / "store.lock",
            standby_mode_file=standby_file,
            failover_lock_file=tmp_path / "failover.lock",
            log_file=tmp_path / "sync.log",
        )

        _create_db(paths.target_db)

        with patch("xui_standby_sync.status.run_command") as mock:
            mock.return_value = MagicMock(returncode=0)
            status = collect_status(paths)

        assert status["standby_marker_secure"] is False

    def test_target_db_integrity_check(self, tmp_path: Path):
        db_path = tmp_path / "x-ui.db"
        _create_db(db_path)

        paths = PathsConfig(
            target_db=db_path,
            allowlist_path=tmp_path / "allowlist.json",
            incoming_dir=tmp_path / "incoming",
            work_root=tmp_path / "work",
            snapshots_dir=tmp_path / "snapshots",
            gnupg_dir=tmp_path / "gnupg",
            sync_lock_path=tmp_path / "sync.lock",
            store_lock_path=tmp_path / "store.lock",
            standby_mode_file=tmp_path / "standby-mode",
            failover_lock_file=tmp_path / "failover.lock",
            log_file=tmp_path / "sync.log",
        )

        with patch("xui_standby_sync.status.run_command") as mock:
            mock.return_value = MagicMock(returncode=0)
            status = collect_status(paths)

        assert status["target_db_integrity"] == "ok"

    def test_xui_service_active_check(self, tmp_path: Path):
        db_path = tmp_path / "x-ui.db"
        _create_db(db_path)

        paths = PathsConfig(
            target_db=db_path,
            allowlist_path=tmp_path / "allowlist.json",
            incoming_dir=tmp_path / "incoming",
            work_root=tmp_path / "work",
            snapshots_dir=tmp_path / "snapshots",
            gnupg_dir=tmp_path / "gnupg",
            sync_lock_path=tmp_path / "sync.lock",
            store_lock_path=tmp_path / "store.lock",
            standby_mode_file=tmp_path / "standby-mode",
            failover_lock_file=tmp_path / "failover.lock",
            log_file=tmp_path / "sync.log",
        )

        with patch("xui_standby_sync.status.run_command") as mock:
            mock.return_value = MagicMock(returncode=0)
            status = collect_status(paths)

        assert status["xui_service_active"] is True

    def test_lock_busy_detection(self, tmp_path: Path):
        db_path = tmp_path / "x-ui.db"
        _create_db(db_path)
        
        sync_lock = tmp_path / "sync.lock"
        sync_lock.write_text("locked")
        os.chmod(sync_lock, 0o600)

        paths = PathsConfig(
            target_db=db_path,
            allowlist_path=tmp_path / "allowlist.json",
            incoming_dir=tmp_path / "incoming",
            work_root=tmp_path / "work",
            snapshots_dir=tmp_path / "snapshots",
            gnupg_dir=tmp_path / "gnupg",
            sync_lock_path=sync_lock,
            store_lock_path=tmp_path / "store.lock",
            standby_mode_file=tmp_path / "standby-mode",
            failover_lock_file=tmp_path / "failover.lock",
            log_file=tmp_path / "sync.log",
        )

        with patch("xui_standby_sync.status.run_command") as mock:
            mock.return_value = MagicMock(returncode=0)
            status = collect_status(paths)

        assert "sync_lock_busy" in status


class TestStatusJson:
    """Test JSON status output."""

    def test_status_json_format(self, tmp_path: Path):
        db_path = tmp_path / "x-ui.db"
        _create_db(db_path)

        paths = PathsConfig(
            target_db=db_path,
            allowlist_path=tmp_path / "allowlist.json",
            incoming_dir=tmp_path / "incoming",
            work_root=tmp_path / "work",
            snapshots_dir=tmp_path / "snapshots",
            gnupg_dir=tmp_path / "gnupg",
            sync_lock_path=tmp_path / "sync.lock",
            store_lock_path=tmp_path / "store.lock",
            standby_mode_file=tmp_path / "standby-mode",
            failover_lock_file=tmp_path / "failover.lock",
            log_file=tmp_path / "sync.log",
        )

        with patch("xui_standby_sync.status.run_command") as mock:
            mock.return_value = MagicMock(returncode=0)
            output = status_json(paths)

        parsed = json.loads(output)
        assert "standby_mode" in parsed
        assert "target_db_integrity" in parsed
        assert "xui_service_active" in parsed
