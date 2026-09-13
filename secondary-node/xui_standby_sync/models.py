"""Immutable data contracts shared by planner, engine, and workflow."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

JSONValue: TypeAlias = (
    str | int | float | bool | list["JSONValue"] | dict[str, "JSONValue"] | None
)


@dataclass(frozen=True)
class PathsConfig:
    target_db: Path
    allowlist_path: Path
    incoming_dir: Path
    work_root: Path
    snapshots_dir: Path
    gnupg_dir: Path
    sync_lock_path: Path
    store_lock_path: Path
    standby_mode_file: Path
    failover_lock_file: Path
    log_file: Path


@dataclass(frozen=True)
class SecurityConfig:
    trusted_gpg_signer_fingerprints: frozenset[str]
    require_gpg_signature: bool
    max_unpack_size_bytes: int
    allow_unsafe_backup_path: bool


@dataclass(frozen=True)
class SyncPolicy:
    service_name: str
    command_timeout: int
    max_age_seconds: int
    max_clock_skew_seconds: int
    max_rollback_copies: int
    custom_reserved_ports: frozenset[int]
    primary_ip: str | None
    standby_ip: str | None


@dataclass(frozen=True)
class NotificationConfig:
    enabled: bool
    bot_token: str | None
    chat_id: str | None
    proxy_url: str | None


@dataclass(frozen=True)
class RuntimeOptions:
    force: bool
    dry_run: bool
    json_output: bool
    verbose: bool
    unsafe_backup_path: bool


@dataclass(frozen=True)
class RuntimeConfig:
    paths: PathsConfig
    security: SecurityConfig
    policy: SyncPolicy
    notifications: NotificationConfig
    options: RuntimeOptions


@dataclass(frozen=True)
class BackupMetadata:
    archive_path: Path
    archive_name: str
    timestamp: datetime
    age_hours: float
    sha256_hash: str
    is_fresh: bool
    signer_fingerprint: str | None
    signature_verified: bool


@dataclass(frozen=True)
class ExistingInboundRef:
    target_id: int


@dataclass(frozen=True)
class NewInboundRef:
    source_id: int


InboundRef: TypeAlias = ExistingInboundRef | NewInboundRef


@dataclass(frozen=True)
class InboundCreateOperation:
    source_inbound_id: int
    values: Mapping[str, JSONValue]


@dataclass(frozen=True)
class InboundClientSettingsUpdateOperation:
    target_inbound_id: int
    settings_json: str


@dataclass(frozen=True)
class InboundDeleteOperation:
    target_inbound_id: int
    port: int
    tag: str | None


@dataclass(frozen=True)
class ClientInsertOperation:
    source_inbound_id: int
    values: Mapping[str, JSONValue]


@dataclass(frozen=True)
class ClientUpdateOperation:
    target_client_id: int
    values: Mapping[str, JSONValue]


@dataclass(frozen=True)
class TrafficInsertOperation:
    source_inbound_id: int
    values: Mapping[str, JSONValue]


@dataclass(frozen=True)
class TrafficUpdateOperation:
    target_traffic_id: int
    values: Mapping[str, JSONValue]


@dataclass(frozen=True)
class SettingsUpdateOperation:
    target_setting_id: int | None
    key: str
    value: str


@dataclass(frozen=True)
class DatabaseFingerprint:
    schema_version: int
    data_version: int


@dataclass(frozen=True)
class DatabaseSyncPlan:
    target_fingerprint: DatabaseFingerprint
    inbound_mapping: tuple[tuple[int, int], ...]
    inbound_creates: tuple[InboundCreateOperation, ...]
    inbound_client_updates: tuple[InboundClientSettingsUpdateOperation, ...]
    inbound_deletes: tuple[InboundDeleteOperation, ...]
    client_inserts: tuple[ClientInsertOperation, ...]
    client_updates: tuple[ClientUpdateOperation, ...]
    client_deletes: tuple[int, ...]
    traffic_inserts: tuple[TrafficInsertOperation, ...]
    traffic_updates: tuple[TrafficUpdateOperation, ...]
    traffic_deletes: tuple[int, ...]
    settings_updates: tuple[SettingsUpdateOperation, ...]
    xray_template_json: str | None
    validation_errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.validation_errors


@dataclass(frozen=True)
class SyncRunPlan:
    backup: BackupMetadata
    database: DatabaseSyncPlan


@dataclass(frozen=True)
class SnapshotMetadata:
    run_id: str
    snapshot_path: Path
    created_at: datetime
    archive_name: str
    archive_sha256: str
    signer_fingerprint: str | None


@dataclass(frozen=True)
class ExecutionResult:
    success: bool
    run_id: str
    plan: SyncRunPlan | None
    service_was_stopped: bool
    service_restarted: bool
    rollback_attempted: bool
    rollback_succeeded: bool | None
    error_message: str | None
    exit_code: int
