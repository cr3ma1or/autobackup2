"""Shared test fixtures and utilities."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import xui_standby_sync.locks as locks_module


@pytest.fixture
def tmp_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a root-owned, private temporary directory for security tests."""
    monkeypatch.setattr(
        locks_module.pwd,
        "getpwnam",
        lambda _name: type("Account", (), {"pw_uid": 0, "pw_gid": 0})(),
    )
    path = Path(tempfile.mkdtemp(prefix="xui-tests-", dir="/root"))
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

from xui_standby_sync.models import (  # noqa: E402 -- deferred import avoids circularity
    BackupMetadata,
    NotificationConfig,
    PathsConfig,
    RuntimeConfig,
    RuntimeOptions,
    SecurityConfig,
    SyncPolicy,
)


@pytest.fixture
def tmp_path_secure(tmp_path: Path) -> Path:
    """Create a temporary directory with secure permissions."""
    os.chmod(tmp_path, 0o700)
    return tmp_path


@pytest.fixture
def mock_paths(tmp_path_secure: Path) -> PathsConfig:
    """Create mock paths configuration with temporary directories."""
    return PathsConfig(
        target_db=tmp_path_secure / "x-ui.db",
        allowlist_path=tmp_path_secure / "allowlist.json",
        incoming_dir=tmp_path_secure / "incoming",
        work_root=tmp_path_secure / "work",
        snapshots_dir=tmp_path_secure / "snapshots",
        gnupg_dir=tmp_path_secure / "gnupg",
        sync_lock_path=tmp_path_secure / "sync.lock",
        store_lock_path=tmp_path_secure / "store.lock",
        standby_mode_file=tmp_path_secure / "standby-mode",
        failover_lock_file=tmp_path_secure / "failover.lock",
        log_file=tmp_path_secure / "sync.log",
    )


@pytest.fixture
def mock_security_config() -> SecurityConfig:
    """Create mock security configuration."""
    return SecurityConfig(
        trusted_gpg_signer_fingerprints=frozenset([
            "ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234"
        ]),
        require_gpg_signature=True,
        max_unpack_size_bytes=500 * 1024 * 1024,
        allow_unsafe_backup_path=False,
    )


@pytest.fixture
def mock_sync_policy() -> SyncPolicy:
    """Create mock sync policy."""
    return SyncPolicy(
        service_name="x-ui",
        command_timeout=30,
        max_age_seconds=6 * 3600,
        max_clock_skew_seconds=300,
        max_rollback_copies=5,
        custom_reserved_ports=frozenset([22, 80, 443]),
        primary_ip="10.0.0.1",
        standby_ip="10.0.0.2",
    )


@pytest.fixture
def mock_notification_config() -> NotificationConfig:
    """Create mock notification configuration."""
    return NotificationConfig(
        enabled=False,
        bot_token=None,
        chat_id=None,
        proxy_url=None,
    )


@pytest.fixture
def mock_runtime_options() -> RuntimeOptions:
    """Create mock runtime options."""
    return RuntimeOptions(
        force=False,
        dry_run=False,
        json_output=False,
        verbose=False,
        unsafe_backup_path=False,
    )


@pytest.fixture
def mock_runtime_config(
    mock_paths: PathsConfig,
    mock_security_config: SecurityConfig,
    mock_sync_policy: SyncPolicy,
    mock_notification_config: NotificationConfig,
    mock_runtime_options: RuntimeOptions,
) -> RuntimeConfig:
    """Create complete mock runtime configuration."""
    return RuntimeConfig(
        paths=mock_paths,
        security=mock_security_config,
        policy=mock_sync_policy,
        notifications=mock_notification_config,
        options=mock_runtime_options,
    )


@pytest.fixture
def sample_allowlist() -> dict:
    """Create a sample allowlist configuration."""
    return {
        "tables": {
            "inbounds": {
                "matching_key": "tag",
                "allowed_columns": [
                    "id", "port", "protocol", "tag", "settings",
                    "stream_settings", "enable", "sniffing"
                ]
            },
            "clients": {
                "matching_key": "uuid",
                "fallback_matching_key": "email",
                "allowed_columns": [
                    "id", "inbound_id", "uuid", "email", "enable",
                    "total_gb", "expiry_time", "sub_id", "tg_id"
                ]
            },
            "client_traffics": {
                "matching_key": "email",
                "allowed_columns": [
                    "id", "inbound_id", "email", "up", "down", "total"
                ]
            },
            "settings": {
                "allowed_keys": [
                    "webPort", "webCertFile", "webKeyFile", "webBasePath",
                    "xrayTemplateConfig", "tgBotEnable", "tgBotToken"
                ]
            }
        },
        "assert_invariants": ["webPort", "webBasePath"]
    }


@pytest.fixture
def create_sqlite_db():
    """Factory to create SQLite databases with test schema."""
    def _create_db(path: Path, *, include_data: bool = True) -> None:
        with sqlite3.connect(path) as conn:
            # Create tables
            conn.execute("""
                CREATE TABLE inbounds (
                    id INTEGER PRIMARY KEY,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL,
                    tag TEXT,
                    settings TEXT NOT NULL,
                    stream_settings TEXT,
                    enable INTEGER DEFAULT 1,
                    sniffing TEXT
                );
            """)
            
            conn.execute("""
                CREATE TABLE clients (
                    id INTEGER PRIMARY KEY,
                    inbound_id INTEGER NOT NULL,
                    uuid TEXT,
                    email TEXT,
                    enable INTEGER DEFAULT 1,
                    total_gb INTEGER DEFAULT 0,
                    expiry_time INTEGER DEFAULT 0,
                    sub_id TEXT,
                    tg_id TEXT,
                    FOREIGN KEY (inbound_id) REFERENCES inbounds(id)
                );
            """)
            
            conn.execute("""
                CREATE TABLE client_traffics (
                    id INTEGER PRIMARY KEY,
                    inbound_id INTEGER NOT NULL,
                    email TEXT NOT NULL,
                    up INTEGER DEFAULT 0,
                    down INTEGER DEFAULT 0,
                    total INTEGER DEFAULT 0,
                    FOREIGN KEY (inbound_id) REFERENCES inbounds(id)
                );
            """)
            
            conn.execute("""
                CREATE TABLE settings (
                    id INTEGER PRIMARY KEY,
                    key TEXT UNIQUE NOT NULL,
                    value TEXT NOT NULL
                );
            """)
            
            if include_data:
                # Insert sample data
                conn.execute("""
                    INSERT INTO inbounds (id, port, protocol, tag, settings, stream_settings)
                    VALUES (1, 443, 'vless', 'primary', 
                           '{"clients": []}', 
                           '{"network": "tcp", "security": "reality"}');
                """)
                
                conn.execute("""
                    INSERT INTO settings (key, value) VALUES 
                    ('webPort', '2053'),
                    ('webBasePath', '/admin/'),
                    ('xrayTemplateConfig', '{"routing": {"rules": []}, "outbounds": []}');
                """)
        
        os.chmod(path, 0o600)
    
    return _create_db


@pytest.fixture
def mock_backup_metadata(tmp_path_secure: Path) -> BackupMetadata:
    """Create mock backup metadata."""
    from datetime import datetime, timezone
    
    return BackupMetadata(
        archive_path=tmp_path_secure / "xui-backup-20261212T120000Z-test.tar.gz.gpg",
        archive_name="xui-backup-20261212T120000Z-test.tar.gz.gpg",
        timestamp=datetime(2026, 12, 12, 12, 0, 0, tzinfo=timezone.utc),
        age_hours=1.0,
        sha256_hash="a" * 64,
        is_fresh=True,
        signer_fingerprint="ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234",
        signature_verified=True,
    )


@pytest.fixture
def mock_command_runner():
    """Mock for run_command function."""
    return MagicMock()


@pytest.fixture
def setup_directories(mock_paths: PathsConfig):
    """Set up required directories with proper permissions."""
    for path in [
        mock_paths.incoming_dir,
        mock_paths.work_root,
        mock_paths.snapshots_dir,
        mock_paths.gnupg_dir,
    ]:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    
    # Create standby mode file
    mock_paths.standby_mode_file.write_text("STANDBY")
    os.chmod(mock_paths.standby_mode_file, 0o600)
    
    # Create allowlist file  
    allowlist = {
        "tables": {
            "inbounds": {
                "matching_key": "tag",
                "allowed_columns": ["id", "port", "tag", "settings"],
            },
            "clients": {
                "matching_key": "uuid",
                "fallback_matching_key": "email",
                "allowed_columns": ["id", "inbound_id", "uuid", "email"],
            },
            "client_traffics": {
                "matching_key": "email",
                "allowed_columns": ["id", "inbound_id", "email", "up", "down"],
            },
            "settings": {"allowed_keys": ["webPort", "webBasePath"]},
        },
        "assert_invariants": ["webPort"],
    }
    mock_paths.allowlist_path.write_text(json.dumps(allowlist))
    os.chmod(mock_paths.allowlist_path, 0o600)


@pytest.fixture(autouse=True)
def reset_environment():
    """Reset environment variables before each test."""
    original_env = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(original_env)