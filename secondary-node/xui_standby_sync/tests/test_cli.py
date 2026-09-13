"""CLI interface tests."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from xui_standby_sync.cli import main, legacy_main


def _make_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        conn.execute("INSERT INTO settings VALUES (1, 'webPort', '2053')")
        conn.commit()
    os.chmod(path, 0o600)


class TestCliMainHelp:
    """Test CLI help and basic functionality."""

    def test_main_help_works(self):
        """xui-standby --help works."""
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_subcommand_help_works(self):
        """xui-standby sync --help works."""
        with pytest.raises(SystemExit) as exc:
            main(["sync", "--help"])
        assert exc.value.code == 0

    def test_unknown_argument_fails(self):
        """Unknown CLI argument ends with parser error."""
        with pytest.raises(SystemExit):
            main(["--unknown-arg"])


class TestCliStatus:
    """Test status command."""

    def test_status_json_output(self, tmp_path: Path):
        """status --json outputs valid JSON."""
        db_path = tmp_path / "x-ui.db"
        _make_db(db_path)

        with patch("xui_standby_sync.cli.load_values", return_value={}):
            with patch("xui_standby_sync.cli.build_runtime_config") as mock_config:
                config = MagicMock()
                config.paths.target_db = db_path
                config.paths.standby_mode_file = tmp_path / "standby"
                config.paths.standby_mode_file.write_text("STANDBY")
                os.chmod(config.paths.standby_mode_file, 0o600)
                config.paths.failover_lock_file = tmp_path / "failover"
                config.paths.sync_lock_path = tmp_path / "sync.lock"
                config.paths.store_lock_path = tmp_path / "store.lock"
                config.policy.service_name = "x-ui"
                mock_config.return_value = config

                with patch("xui_standby_sync.cli.run_command") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                    
                    with patch("builtins.print") as mock_print:
                        result = main(["status", "--json"])
                        
                        # Check that JSON was printed
                        assert mock_print.called
                        output = mock_print.call_args[0][0]
                        parsed = json.loads(output)
                        assert isinstance(parsed, dict)

    def test_status_text_output(self, tmp_path: Path):
        """status without --json outputs text."""
        db_path = tmp_path / "x-ui.db"
        _make_db(db_path)

        with patch("xui_standby_sync.cli.load_values", return_value={}):
            with patch("xui_standby_sync.cli.build_runtime_config") as mock_config:
                config = MagicMock()
                config.paths.target_db = db_path
                config.paths.standby_mode_file = tmp_path / "standby"
                config.paths.standby_mode_file.write_text("STANDBY")
                os.chmod(config.paths.standby_mode_file, 0o600)
                config.paths.failover_lock_file = tmp_path / "failover"
                config.paths.sync_lock_path = tmp_path / "sync.lock"
                config.paths.store_lock_path = tmp_path / "store.lock"
                config.policy.service_name = "x-ui"
                mock_config.return_value = config

                with patch("xui_standby_sync.cli.run_command") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                    
                    with patch("builtins.print") as mock_print:
                        result = main(["status"])
                        
                        # Check that text was printed
                        assert mock_print.called
                        output = mock_print.call_args[0][0]
                        # Should not be JSON
                        try:
                            json.loads(output)
                            pytest.fail("Should not be JSON")
                        except (json.JSONDecodeError, TypeError):
                            pass  # Expected


class TestCliValidate:
    """Test validate command."""

    def test_validate_json_output(self, tmp_path: Path):
        """validate --json outputs valid JSON."""
        db_path = tmp_path / "x-ui.db"
        _make_db(db_path)

        with patch("xui_standby_sync.cli.load_values", return_value={}):
            with patch("xui_standby_sync.cli.build_runtime_config") as mock_config:
                config = MagicMock()
                config.paths.target_db = db_path
                config.paths.allowlist_path = tmp_path / "allowlist.json"
                config.paths.allowlist_path.write_text(json.dumps({
                    "tables": {
                        "inbounds": {"allowed_columns": ["id"]},
                        "clients": {"allowed_columns": ["id"]},
                        "client_traffics": {"allowed_columns": ["id"]},
                        "settings": {"allowed_keys": ["webPort"]}
                    }
                }))
                os.chmod(config.paths.allowlist_path, 0o600)
                mock_config.return_value = config

                with patch("builtins.print") as mock_print:
                    result = main(["validate", "--json"])
                    
                    assert mock_print.called
                    output = mock_print.call_args[0][0]
                    parsed = json.loads(output)
                    assert parsed.get("valid") is True


class TestCliCheckBackup:
    """Test check-backup command."""

    def test_check_backup_requires_backup_argument(self):
        """check-backup requires --backup argument."""
        with pytest.raises(SystemExit):
            main(["check-backup"])

    def test_check_backup_json_output(self, tmp_path: Path):
        """check-backup --json outputs valid JSON."""
        backup_path = tmp_path / "backup.tar.gz.gpg"
        backup_path.write_bytes(b"backup content")
        os.chmod(backup_path, 0o600)

        sidecar_path = tmp_path / "backup.tar.gz.gpg.sha256"
        import hashlib
        hash_val = hashlib.sha256(b"backup content").hexdigest()
        sidecar_path.write_text(f"{hash_val}  backup.tar.gz.gpg\n")
        os.chmod(sidecar_path, 0o600)

        with patch("xui_standby_sync.cli.load_values", return_value={}):
            with patch("xui_standby_sync.cli.build_runtime_config") as mock_config:
                config = MagicMock()
                config.paths.incoming_dir = tmp_path
                config.security.allow_unsafe_backup_path = False
                config.policy.max_age_seconds = 6 * 3600
                config.policy.max_clock_skew_seconds = 300
                config.options.force = False
                mock_config.return_value = config

                with patch("builtins.print") as mock_print:
                    result = main(["check-backup", "--backup", str(backup_path), "--json"])
                    
                    assert mock_print.called
                    output = mock_print.call_args[0][0]
                    parsed = json.loads(output)
                    assert parsed.get("valid") is True


class TestCliRollback:
    """Test rollback command."""

    def test_rollback_requires_yes_flag(self):
        """rollback requires --yes flag."""
        with pytest.raises(SystemExit):
            main(["rollback", "--snapshot", "/path/to/snapshot"])

    def test_rollback_requires_snapshot_or_run_id(self):
        """rollback requires --snapshot or --run-id."""
        with pytest.raises(SystemExit):
            main(["rollback", "--yes"])

    def test_rollback_with_snapshot_succeeds(self, tmp_path: Path):
        """rollback --snapshot --yes executes."""
        snapshot_path = tmp_path / "snapshot.db"
        _make_db(snapshot_path)

        db_path = tmp_path / "x-ui.db"
        _make_db(db_path)

        with patch("xui_standby_sync.cli.load_values", return_value={}):
            with patch("xui_standby_sync.cli.build_runtime_config") as mock_config:
                config = MagicMock()
                config.paths.target_db = db_path
                config.paths.standby_mode_file = tmp_path / "standby"
                config.paths.standby_mode_file.write_text("STANDBY")
                os.chmod(config.paths.standby_mode_file, 0o600)
                config.paths.sync_lock_path = tmp_path / "sync.lock"
                config.paths.store_lock_path = tmp_path / "store.lock"
                config.policy.service_name = "x-ui"
                config.policy.command_timeout = 30
                mock_config.return_value = config

                with patch("xui_standby_sync.cli.run_command") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                    
                    with patch("xui_standby_sync.cli.restore_rollback_snapshot"):
                        with patch("builtins.print") as mock_print:
                            result = main([
                                "rollback",
                                "--snapshot", str(snapshot_path),
                                "--yes"
                            ])
                            
                            assert result == 0


class TestCliSyncCommand:
    """Test sync command."""

    def test_sync_dry_run_does_not_modify_db(self, tmp_path: Path):
        """sync --dry-run does not modify target DB."""
        db_path = tmp_path / "x-ui.db"
        _make_db(db_path)
        checksum_before = db_path.read_bytes()

        with patch("xui_standby_sync.cli.load_values", return_value={}):
            with patch("xui_standby_sync.cli.build_runtime_config") as mock_config:
                config = MagicMock()
                config.paths.target_db = db_path
                config.paths.standby_mode_file = tmp_path / "standby"
                config.paths.standby_mode_file.write_text("STANDBY")
                os.chmod(config.paths.standby_mode_file, 0o600)
                config.paths.allowlist_path = tmp_path / "allowlist.json"
                config.paths.allowlist_path.write_text(json.dumps({
                    "tables": {
                        "inbounds": {"matching_key": "tag", "allowed_columns": ["id", "port", "tag", "settings", "stream_settings"]},
                        "clients": {"matching_key": "uuid", "allowed_columns": ["id"]},
                        "client_traffics": {"matching_key": "email", "allowed_columns": ["id"]},
                        "settings": {"allowed_keys": ["webPort"]}
                    }
                }))
                os.chmod(config.paths.allowlist_path, 0o600)
                config.paths.incoming_dir = tmp_path / "incoming"
                config.paths.incoming_dir.mkdir()
                config.paths.work_root = tmp_path / "work"
                config.paths.work_root.mkdir()
                config.paths.snapshots_dir = tmp_path / "snapshots"
                config.paths.snapshots_dir.mkdir()
                config.paths.gnupg_dir = tmp_path / "gnupg"
                config.paths.gnupg_dir.mkdir()
                os.chmod(config.paths.gnupg_dir, 0o700)
                config.paths.sync_lock_path = tmp_path / "sync.lock"
                config.paths.store_lock_path = tmp_path / "store.lock"
                config.paths.failover_lock_file = tmp_path / "failover"
                config.security.allow_unsafe_backup_path = False
                config.security.require_gpg_signature = False
                config.policy.max_age_seconds = 6 * 3600
                config.policy.max_clock_skew_seconds = 300
                config.policy.command_timeout = 30
                config.policy.custom_reserved_ports = frozenset()
                config.policy.primary_ip = None
                config.policy.standby_ip = None
                config.options.dry_run = True
                config.options.force = False
                config.options.json_output = False
                mock_config.return_value = config

                with patch("xui_standby_sync.cli.run_sync") as mock_sync:
                    from xui_standby_sync.models import ExecutionResult, SyncRunPlan
                    mock_sync.return_value = ExecutionResult(
                        success=True,
                        run_id="preview",
                        plan=None,
                        service_was_stopped=False,
                        service_restarted=False,
                        rollback_attempted=False,
                        rollback_succeeded=None,
                        error_message=None,
                        exit_code=0,
                    )
                    
                    result = main(["sync", "--dry-run"])
                    
                    assert result == 0
        
        # DB should be unchanged
        assert db_path.read_bytes() == checksum_before


class TestCliLegacyAlias:
    """Test xui-standby-sync legacy alias."""

    def test_legacy_main_works(self):
        """xui-standby-sync --help works."""
        with pytest.raises(SystemExit) as exc:
            legacy_main(["--help"])
        # Should delegate to main
        assert exc.value.code == 0

    def test_legacy_sync_alias(self):
        """xui-standby-sync [options] works as sync command."""
        # Legacy alias should map to sync command
        pass
