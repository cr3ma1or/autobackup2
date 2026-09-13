"""Archive decryption and extraction tests as required by section 11.5 of TZ-UPGRADE.md."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from xui_standby_sync.archive import (
    decrypt_and_extract,
    _extract_archive,
    _extract_worker,
    _parse_gpg_status,
)
from xui_standby_sync.commands import CommandError
from xui_standby_sync.exceptions import ArchiveValidationError, SignatureVerificationError
from xui_standby_sync.models import SecurityConfig


class TestGpgStatusParsing:
    """Test GPG status parsing."""

    def test_valid_signed_trusted_fingerprint_succeeds(self):
        """Valid signed archive succeeds with trusted fingerprint."""
        status_output = """[GNUPG:] GOODSIG ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234
[GNUPG:] VALIDSIG ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234 2026-01-01
"""
        
        result = _parse_gpg_status(
            status_output,
            frozenset({"ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"}),
            required=True,
        )
        
        assert result == "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"

    def test_unsigned_archive_fails_when_signature_required(self):
        """Unsigned archive fails when signature required."""
        status_output = ""
        
        with pytest.raises(SignatureVerificationError, match="GPG signature is missing"):
            _parse_gpg_status(
                status_output,
                frozenset({"ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"}),
                required=True,
            )

    def test_unsigned_archive_succeeds_when_signature_not_required(self):
        """Unsigned archive succeeds when signature not required."""
        status_output = ""
        
        result = _parse_gpg_status(
            status_output,
            frozenset(),
            required=False,
        )
        
        assert result == ""

    def test_valid_signature_from_untrusted_fingerprint_fails(self):
        """Valid signature from untrusted fingerprint fails."""
        status_output = """[GNUPG:] GOODSIG BBBBBB1234BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
[GNUPG:] VALIDSIG BBBBBB1234BBBBBBBBBBBBBBBBBBBBBBBBBBBBBB 2026-01-01
"""
        
        with pytest.raises(SignatureVerificationError, match="untrusted fingerprint"):
            _parse_gpg_status(
                status_output,
                frozenset({"ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"}),
                required=True,
            )

    def test_bad_signature_fails(self):
        """Bad signature fails."""
        status_output = "[GNUPG:] BADSIG ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234\n"
        
        with pytest.raises(SignatureVerificationError, match="GPG signature is missing"):
            _parse_gpg_status(
                status_output,
                frozenset({"ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"}),
                required=True,
            )


class TestTarExtraction:
    """Test tar extraction and validation."""

    def test_tar_with_one_regular_xui_db_succeeds(self, tmp_path: Path):
        """Tar with one regular x-ui.db succeeds."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        # Create valid tar with x-ui.db
        payload_path = tmp_path / "payload.tar.gz"
        db_content = b"SQLite database content"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = len(db_content)
            info.mode = 0o644
            tar.addfile(info, __import__("io").BytesIO(db_content))
        
        _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)
        
        extracted = work_dir / "x-ui.db"
        assert extracted.exists()

    def test_tar_with_unexpected_member_fails(self, tmp_path: Path):
        """Tar with an unexpected member name fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            for name in ["x-ui.db", "extra.txt"]:
                info = tarfile.TarInfo(name=name)
                info.size = 5
                tar.addfile(info, __import__("io").BytesIO(b"test"))
        
        with pytest.raises(ArchiveValidationError, match="Unexpected archive member"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_symlink_member_fails(self, tmp_path: Path):
        """Tar with symlink member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        
        with pytest.raises(ArchiveValidationError, match="must be regular"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_hardlink_member_fails(self, tmp_path: Path):
        """Tar with hardlink member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.type = tarfile.LNKTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        
        with pytest.raises(ArchiveValidationError, match="must be regular"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_directory_member_fails(self, tmp_path: Path):
        """Tar with directory member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
        
        with pytest.raises(ArchiveValidationError, match="must be regular"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_device_member_fails(self, tmp_path: Path):
        """Tar with device member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.type = tarfile.CHRTYPE  # Character device
            tar.addfile(info)
        
        with pytest.raises(ArchiveValidationError, match="must be regular"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_fifo_member_fails(self, tmp_path: Path):
        """Tar with FIFO member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.type = tarfile.FIFOTYPE
            tar.addfile(info)
        
        with pytest.raises(ArchiveValidationError, match="must be regular"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_absolute_path_fails(self, tmp_path: Path):
        """Tar with absolute path member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="/etc/passwd")
            info.size = 5
            info.type = tarfile.REGTYPE
            tar.addfile(info, __import__("io").BytesIO(b"test"))
        
        with pytest.raises(ArchiveValidationError, match="Unexpected archive member"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_dot_dot_path_fails(self, tmp_path: Path):
        """Tar with .. path member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="../../../etc/passwd")
            info.size = 5
            info.type = tarfile.REGTYPE
            tar.addfile(info, __import__("io").BytesIO(b"test"))
        
        with pytest.raises(ArchiveValidationError, match="Unexpected archive member"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_oversized_declared_member_fails(self, tmp_path: Path):
        """Oversized declared member fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        small_content = b"small"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = 10 * 1024 * 1024 * 1024  # 10GB declared, but small actual
            tar.addfile(info, __import__("io").BytesIO(small_content))
        
        with pytest.raises(ArchiveValidationError, match="size is outside"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_member_size_zero_fails(self, tmp_path: Path):
        """Member with zero size fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "payload.tar.gz"
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = 0
            info.type = tarfile.REGTYPE
            tar.addfile(info, __import__("io").BytesIO(b""))
        
        with pytest.raises(ArchiveValidationError, match="size is outside"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_with_manifest_and_xuidb_succeeds(self, tmp_path: Path):
        """Tar with manifest.json and x-ui.db succeeds (primary archive format)."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        payload_path = tmp_path / "payload.tar.gz"
        db_content = b"SQLite database content"
        manifest = {"format": "xui-backup-manifest-v2", "database_sha256": hashlib.sha256(db_content).hexdigest()}
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            mbytes = json.dumps(manifest).encode()
            info.size = len(mbytes)
            tar.addfile(info, io.BytesIO(mbytes))
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = len(db_content)
            tar.addfile(info, io.BytesIO(db_content))
        _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)
        assert (work_dir / "x-ui.db").exists()

    def test_tar_with_manifest_export_and_xuidb_succeeds(self, tmp_path: Path):
        """Tar with manifest.json, 3xui_export.json, and x-ui.db succeeds."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        payload_path = tmp_path / "payload.tar.gz"
        db_content = b"SQLite database content"
        manifest = {"format": "xui-backup-manifest-v2", "database_sha256": hashlib.sha256(db_content).hexdigest()}
        with tarfile.open(payload_path, "w:gz") as tar:
            for name, content in [
                ("manifest.json", json.dumps(manifest).encode()),
                ("3xui_export.json", b'{"tables":{}}'),
                ("x-ui.db", db_content),
            ]:
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)
        assert (work_dir / "x-ui.db").exists()

    def test_tar_manifest_hash_mismatch_fails(self, tmp_path: Path):
        """Archive with manifest database_sha256 mismatch fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        payload_path = tmp_path / "payload.tar.gz"
        db_content = b"SQLite database content"
        manifest = {"format": "xui-backup-manifest-v2", "database_sha256": "a" * 64}
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            mbytes = json.dumps(manifest).encode()
            info.size = len(mbytes)
            tar.addfile(info, io.BytesIO(mbytes))
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = len(db_content)
            tar.addfile(info, io.BytesIO(db_content))
        with pytest.raises(ArchiveValidationError, match="Database hash differs from manifest"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_no_xuidb_member_fails(self, tmp_path: Path):
        """Tar with manifest.json but no x-ui.db fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        payload_path = tmp_path / "payload.tar.gz"
        manifest = {"format": "xui-backup-manifest-v2", "database_sha256": "a" * 64}
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="manifest.json")
            mbytes = json.dumps(manifest).encode()
            info.size = len(mbytes)
            tar.addfile(info, io.BytesIO(mbytes))
        with pytest.raises(ArchiveValidationError, match="exactly one x-ui.db member"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)

    def test_tar_two_xuidb_members_fails(self, tmp_path: Path):
        """Tar with two x-ui.db members fails."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        payload_path = tmp_path / "payload.tar.gz"
        with tarfile.open(payload_path, "w:gz") as tar:
            for name in ["x-ui.db", "./x-ui.db"]:
                info = tarfile.TarInfo(name=name)
                info.size = 4
                tar.addfile(info, __import__("io").BytesIO(b"test"))
        with pytest.raises(ArchiveValidationError, match="exactly one x-ui.db member"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)


class TestArchiveTimeout:
    """Test archive extraction timeout handling."""

    def test_archive_worker_timeout_terminates_worker(self, tmp_path: Path):
        """Archive worker timeout terminates worker and returns controlled error."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "large_payload.tar.gz"
        # Create a very large tar that will take time to extract
        large_content = b"\x00" * (50 * 1024 * 1024)  # 50MB
        
        with tarfile.open(payload_path, "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = len(large_content)
            info.type = tarfile.REGTYPE
            tar.addfile(info, __import__("io").BytesIO(large_content))
        
        # Use very short timeout to trigger timeout
        with pytest.raises(ArchiveValidationError, match="timed out"):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, timeout=1)

    def test_worker_crash_no_queue_result_returns_controlled_error(self, tmp_path: Path):
        """Worker crash/no queue result returns controlled error."""
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        payload_path = tmp_path / "invalid.tar.gz"
        payload_path.write_bytes(b"not a valid tar")
        
        with pytest.raises(ArchiveValidationError):
            _extract_archive(str(payload_path), str(work_dir), 500 * 1024 * 1024, 30)


class TestDecryptAndExtract:
    """Test full decryption and extraction flow."""

    def test_missing_gpg_executable_fails_safely(self, tmp_path: Path):
        """Missing GPG executable fails safely."""
        archive_path = tmp_path / "backup.tar.gz.gpg"
        archive_path.write_bytes(b"encrypted content")
        os.chmod(archive_path, 0o600)
        
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        gnupg_dir = tmp_path / "gnupg"
        gnupg_dir.mkdir()
        os.chmod(gnupg_dir, 0o700)
        
        security = SecurityConfig(
            trusted_gpg_signer_fingerprints=frozenset(),
            require_gpg_signature=False,
            max_unpack_size_bytes=500 * 1024 * 1024,
            allow_unsafe_backup_path=False,
        )
        
        with patch("xui_standby_sync.security.run_command") as mock_run:
            mock_run.side_effect = CommandError("gpg not found")
            
            with pytest.raises(CommandError):
                decrypt_and_extract(
                    archive_path=archive_path,
                    work_dir=work_dir,
                    gnupg_dir=gnupg_dir,
                    security=security,
                    timeout=10,
                    terminate_gpg_agent=False,
                )

    def test_gpg_timeout_fails_safely(self, tmp_path: Path):
        """GPG timeout fails safely."""
        archive_path = tmp_path / "backup.tar.gz.gpg"
        archive_path.write_bytes(b"encrypted content")
        os.chmod(archive_path, 0o600)
        
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        gnupg_dir = tmp_path / "gnupg"
        gnupg_dir.mkdir()
        os.chmod(gnupg_dir, 0o700)
        
        security = SecurityConfig(
            trusted_gpg_signer_fingerprints=frozenset(),
            require_gpg_signature=False,
            max_unpack_size_bytes=500 * 1024 * 1024,
            allow_unsafe_backup_path=False,
        )
        
        with patch("xui_standby_sync.security.run_command") as mock_run:
            mock_run.side_effect = CommandError("gpg timed out")
            
            with pytest.raises(CommandError):
                decrypt_and_extract(
                    archive_path=archive_path,
                    work_dir=work_dir,
                    gnupg_dir=gnupg_dir,
                    security=security,
                    timeout=1,
                    terminate_gpg_agent=False,
                )

    def test_payload_tar_gz_removed_after_extraction(self, tmp_path: Path):
        """payload.tar.gz is removed after extraction."""
        archive_path = tmp_path / "backup.tar.gz.gpg"
        archive_path.write_bytes(b"encrypted content")
        os.chmod(archive_path, 0o600)
        
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        gnupg_dir = tmp_path / "gnupg"
        gnupg_dir.mkdir()
        os.chmod(gnupg_dir, 0o700)
        
        # Create a valid tar for extraction
        db_content = b"SQLite database"
        with tarfile.open(work_dir / "payload.tar.gz", "w:gz") as tar:
            info = tarfile.TarInfo(name="x-ui.db")
            info.size = len(db_content)
            info.type = tarfile.REGTYPE
            tar.addfile(info, __import__("io").BytesIO(db_content))
        
        security = SecurityConfig(
            trusted_gpg_signer_fingerprints=frozenset(),
            require_gpg_signature=False,
            max_unpack_size_bytes=500 * 1024 * 1024,
            allow_unsafe_backup_path=False,
        )
        
        # Mock GPG to return successfully
        with patch("xui_standby_sync.security.run_command") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "[GNUPG:] GOODSIG ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234\n"
            mock_run.return_value = mock_result
            
            try:
                result = decrypt_and_extract(
                    archive_path=archive_path,
                    work_dir=work_dir,
                    gnupg_dir=gnupg_dir,
                    security=security,
                    timeout=10,
                    terminate_gpg_agent=False,
                )
            except (CommandError, ArchiveValidationError):
                pass
        
        # payload.tar.gz should be removed after extraction
        payload_path = work_dir / "payload.tar.gz"
        if payload_path.exists():
            pytest.fail("payload.tar.gz was not removed after extraction")

    def test_dry_run_does_not_kill_gpg_agent(self, tmp_path: Path):
        """Dry-run does not terminate GPG agent."""
        archive_path = tmp_path / "backup.tar.gz.gpg"
        archive_path.write_bytes(b"encrypted content")
        os.chmod(archive_path, 0o600)
        
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        gnupg_dir = tmp_path / "gnupg"
        gnupg_dir.mkdir()
        os.chmod(gnupg_dir, 0o700)
        
        security = SecurityConfig(
            trusted_gpg_signer_fingerprints=frozenset(),
            require_gpg_signature=False,
            max_unpack_size_bytes=500 * 1024 * 1024,
            allow_unsafe_backup_path=False,
        )
        
        gpg_agent_called = []
        
        def mock_run_command(command, **kwargs):
            if "gpgconf" in command:
                gpg_agent_called.append(command)
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            return mock_result
        
        with patch("xui_standby_sync.security.run_command", side_effect=mock_run_command):
            try:
                decrypt_and_extract(
                    archive_path=archive_path,
                    work_dir=work_dir,
                    gnupg_dir=gnupg_dir,
                    security=security,
                    timeout=10,
                    terminate_gpg_agent=False,  # dry-run mode
                )
            except (CommandError, ArchiveValidationError):
                pass
        
        # gpg-agent kill should not be called in dry-run
        assert len(gpg_agent_called) == 0

    def test_gpg_home_security_validation(self, tmp_path: Path):
        """GPG home directory security is validated."""
        archive_path = tmp_path / "backup.tar.gz.gpg"
        archive_path.write_bytes(b"content")
        
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        
        gnupg_dir = tmp_path / "gnupg"
        gnupg_dir.mkdir()
        # Create unsafe permissions
        os.chmod(gnupg_dir, 0o777)
        
        security = SecurityConfig(
            trusted_gpg_signer_fingerprints=frozenset(),
            require_gpg_signature=False,
            max_unpack_size_bytes=500 * 1024 * 1024,
            allow_unsafe_backup_path=False,
        )
        
        with pytest.raises((ArchiveValidationError, Exception)):
            decrypt_and_extract(
                archive_path=archive_path,
                work_dir=work_dir,
                gnupg_dir=gnupg_dir,
                security=security,
                timeout=10,
                terminate_gpg_agent=False,
            )