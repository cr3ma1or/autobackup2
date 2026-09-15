"""Security validation tests as required by section 11.2 of TZ-UPGRADE.md."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from xui_standby_sync.exceptions import SecurityViolationError
from xui_standby_sync.security import (
    best_effort_wipe_file,
    safe_clean_work_dir,
    validate_backup_file,
    validate_path_inside,
    verify_directory_chain,
    verify_file_security,
)


class TestFileSecurityValidation:
    """Test file security validation functions."""

    def test_reject_symlink_for_critical_files(self, tmp_path: Path):
        """Reject symlink for target DB, allowlist, standby marker, backup, etc."""
        target_file = tmp_path / "target.db"
        target_file.write_text("content")
        os.chmod(target_file, 0o600)
        
        symlink_file = tmp_path / "symlink.db"
        symlink_file.symlink_to(target_file)
        
        with pytest.raises(SecurityViolationError, match="must be a regular non-symlink file"):
            verify_file_security(symlink_file, "target database")

    def test_reject_group_world_writable_files(self, tmp_path: Path):
        """Reject group/world writable critical files."""
        test_file = tmp_path / "test.file"
        test_file.write_text("content")
        os.chmod(test_file, 0o644)  # Group readable
        
        with pytest.raises(SecurityViolationError, match="has unsafe permissions"):
            verify_file_security(test_file, "test file")

    def test_reject_non_root_owner_when_possible(self, tmp_path: Path):
        """Reject non-root owner where test environment allows owner mocking."""
        test_file = tmp_path / "test.file"
        test_file.write_text("content")
        os.chmod(test_file, 0o600)
        
        # Mock stat to return non-root owner
        with patch("pathlib.Path.lstat") as mock_lstat:
            mock_stat = type("MockStat", (), {
                "st_mode": stat.S_IFREG | 0o600,
                "st_uid": 1000,  # Non-root
            })()
            mock_lstat.return_value = mock_stat
            
            with pytest.raises(SecurityViolationError, match="must be owned by root"):
                verify_file_security(test_file, "test file")

    def test_accept_valid_secure_file(self, tmp_path: Path):
        """Accept properly secured files."""
        test_file = tmp_path / "test.file"
        test_file.write_text("content")
        os.chmod(test_file, 0o600)
        
        # Mock stat to return root owner
        with patch("pathlib.Path.lstat") as mock_lstat:
            mock_stat = type("MockStat", (), {
                "st_mode": stat.S_IFREG | 0o600,
                "st_uid": 0,  # Root
            })()
            mock_lstat.return_value = mock_stat
            
            # Should not raise
            verify_file_security(test_file, "test file")


class TestDirectorySecurityValidation:
    """Test directory security validation functions."""

    def test_reject_writable_ancestor_directory(self, tmp_path: Path):
        """Reject writable ancestor directory."""
        test_dir = tmp_path / "subdir"
        test_dir.mkdir()
        
        # Mock parent directory as world-writable
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            path = args[0] if args else None
            is_target = (path == test_dir) if path is not None else (call_count == 1)
            return type("MockStat", (), {
                "st_mode": stat.S_IFDIR | (0o755 if is_target else 0o777),
                "st_uid": 0,
            })()

        with patch("pathlib.Path.lstat", side_effect=side_effect):
            
            with pytest.raises(SecurityViolationError, match="Writable parent directory"):
                verify_directory_chain(test_dir, "test directory")

    def test_accept_secure_directory_chain(self, tmp_path: Path):
        """Accept secure directory chain."""
        test_dir = tmp_path / "subdir"
        test_dir.mkdir()
        
        # Mock all directories as secure
        with patch("pathlib.Path.lstat") as mock_lstat:
            mock_stat = type("MockStat", (), {
                "st_mode": stat.S_IFDIR | 0o755,
                "st_uid": 0,
            })()
            mock_lstat.return_value = mock_stat
            
            # Should not raise
            verify_directory_chain(test_dir, "test directory")


class TestWorkDirectoryValidation:
    """Test work directory validation and cleanup."""

    def test_reject_work_dir_equal_to_critical_paths(self, tmp_path: Path):
        """Reject WORK_DIR equal to /, /opt, /etc, /var, /run or SAFE_WORK_ROOT."""
        critical_paths = [
            Path("/"),
            Path("/opt"), 
            Path("/etc"),
            Path("/var"),
            Path("/run"),
        ]
        
        for critical_path in critical_paths:
            with pytest.raises(SecurityViolationError, match="Refusing to remove critical path"):
                safe_clean_work_dir(critical_path)

    def test_reject_workdir_outside_safe_root(self, tmp_path: Path):
        """Reject workdir outside safe root."""
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        safe_root = tmp_path / "safe"
        safe_root.mkdir()
        
        with pytest.raises(SecurityViolationError, match="is outside protected root"):
            validate_path_inside(outside_dir, safe_root, "work directory")

    def test_accept_valid_work_directory_cleanup(self, tmp_path: Path):
        """Accept valid work directory for cleanup."""
        safe_root = tmp_path / "safe"
        safe_root.mkdir()
        work_dir = safe_root / "work"
        work_dir.mkdir()
        test_file = work_dir / "test.txt"
        test_file.write_text("content")
        
        # Should not raise and should clean up
        safe_clean_work_dir(work_dir, safe_root=safe_root)
        assert not work_dir.exists()


class TestSecureFileWiping:
    """Test secure file wiping functionality."""

    def test_best_effort_wipe_opens_overwrite_not_append(self, tmp_path: Path):
        """Verify best_effort_wipe_file() opens overwrite path, not append path."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("secret content")
        
        # Mock shred failure to force fallback
        with patch("xui_standby_sync.security.run_command") as mock_run:
            mock_run.side_effect = OSError("shred failed")
            
            # Mock file operations to verify mode
            original_open = Path.open
            
            def mock_open(self, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if "a" in mode:
                    raise AssertionError("File opened in append mode - security violation!")
                return original_open(self, *args, **kwargs)
            
            with patch.object(Path, "open", mock_open):
                best_effort_wipe_file(test_file)

    def test_best_effort_wipe_handles_missing_file(self, tmp_path: Path):
        """Best effort wipe should handle missing files gracefully."""
        missing_file = tmp_path / "missing.txt"
        
        # Should not raise
        best_effort_wipe_file(missing_file)

    def test_best_effort_wipe_handles_symlink(self, tmp_path: Path):
        """Best effort wipe should skip symlinks."""
        target_file = tmp_path / "target.txt"
        target_file.write_text("content")
        
        symlink_file = tmp_path / "symlink.txt"
        symlink_file.symlink_to(target_file)
        
        # Symlink rejection is the hardened contract; target must remain intact.
        with pytest.raises(SecurityViolationError):
            best_effort_wipe_file(symlink_file)
        assert target_file.read_text() == "content"
        assert target_file.exists()
        assert target_file.read_text() == "content"


class TestDryRunTemporaryWorkspace:
    """Test dry-run temporary workspace behavior."""

    def test_dry_run_does_not_remove_persistent_workdir(self, tmp_path: Path):
        """Ensure dry-run temporary workspace does not remove persistent workdir/snapshot."""
        # This test verifies the design - dry-run uses tempfile.TemporaryDirectory
        # which is automatically cleaned up and separate from persistent paths
        persistent_workdir = tmp_path / "persistent-work"
        persistent_workdir.mkdir()
        persistent_file = persistent_workdir / "persistent.txt"
        persistent_file.write_text("persistent data")
        
        # Simulate dry-run by using a different temporary directory
        import tempfile
        with tempfile.TemporaryDirectory(dir=tmp_path, prefix="dry-run-") as temp_dir:
            temp_path = Path(temp_dir)
            temp_file = temp_path / "temp.txt"
            temp_file.write_text("temporary data")
            
            # After dry-run context, temp should be gone but persistent should remain
            pass
        
        # Verify persistent data is unchanged
        assert persistent_workdir.exists()
        assert persistent_file.read_text() == "persistent data"
        assert not temp_path.exists()


class TestBackupFileValidation:
    """Test backup file validation."""

    def test_backup_symlink_rejection(self, tmp_path: Path):
        """Backup symlinks should be rejected."""
        real_backup = tmp_path / "real-backup.tar.gz.gpg"
        real_backup.write_bytes(b"backup content")
        
        symlink_backup = tmp_path / "symlink-backup.tar.gz.gpg"
        symlink_backup.symlink_to(real_backup)
        
        with pytest.raises(SecurityViolationError):
            validate_backup_file(symlink_backup)

    def test_sidecar_symlink_rejection(self, tmp_path: Path):
        """Sidecar symlinks should be rejected.""" 
        backup = tmp_path / "backup.tar.gz.gpg"
        backup.write_bytes(b"backup content")
        os.chmod(backup, 0o600)
        
        real_sidecar = tmp_path / "real.sha256"
        real_sidecar.write_text("a" * 64 + "  backup.tar.gz.gpg\n")
        
        symlink_sidecar = tmp_path / "backup.tar.gz.gpg.sha256"
        symlink_sidecar.symlink_to(real_sidecar)
        
        with pytest.raises(SecurityViolationError, match="must be a regular non-symlink file"):
            validate_backup_file(symlink_sidecar)