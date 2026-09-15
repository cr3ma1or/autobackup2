"""Immutable data contracts shared by planner, engine, and workflow."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

JSONValue: TypeAlias = str | int | float | bool | tuple["JSONValue", ...] | Mapping[str, "JSONValue"] | None


def _freeze_json(value: Any) -> JSONValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise ValueError(f"Unsupported JSON value type: {type(value).__name__}")


def _freeze_values(values: Mapping[str, Any], *, forbid_id: bool = False) -> Mapping[str, JSONValue]:
    if not isinstance(values, Mapping) or not values:
        raise ValueError("Operation values must be a non-empty mapping")
    if forbid_id and "id" in values:
        raise ValueError("Operations must not mutate primary key 'id'")
    if any(not isinstance(key, str) or not key for key in values):
        raise ValueError("Operation value keys must be non-empty strings")
    return MappingProxyType({key: _freeze_json(value) for key, value in values.items()})


def _positive_id(value: int, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


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

    def __post_init__(self) -> None:
        _positive_id(self.source_inbound_id, "source_inbound_id")
        object.__setattr__(self, "values", _freeze_values(self.values, forbid_id=False))


@dataclass(frozen=True)
class InboundClientSettingsUpdateOperation:
    target_inbound_id: int
    settings_json: str

    def __post_init__(self) -> None:
        _positive_id(self.target_inbound_id, "target_inbound_id")
        if not isinstance(self.settings_json, str) or not self.settings_json:
            raise ValueError("settings_json must be non-empty")


@dataclass(frozen=True)
class InboundDeleteOperation:
    target_inbound_id: int
    port: int
    tag: str | None

    def __post_init__(self) -> None:
        _positive_id(self.target_inbound_id, "target_inbound_id")


@dataclass(frozen=True)
class ClientInsertOperation:
    source_inbound_id: int
    values: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _positive_id(self.source_inbound_id, "source_inbound_id")
        object.__setattr__(self, "values", _freeze_values(self.values, forbid_id=True))


@dataclass(frozen=True)
class ClientUpdateOperation:
    target_client_id: int
    values: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _positive_id(self.target_client_id, "target_client_id")
        object.__setattr__(self, "values", _freeze_values(self.values, forbid_id=True))


@dataclass(frozen=True)
class TrafficInsertOperation:
    source_inbound_id: int
    values: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _positive_id(self.source_inbound_id, "source_inbound_id")
        object.__setattr__(self, "values", _freeze_values(self.values, forbid_id=True))


@dataclass(frozen=True)
class TrafficUpdateOperation:
    target_traffic_id: int
    values: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        _positive_id(self.target_traffic_id, "target_traffic_id")
        object.__setattr__(self, "values", _freeze_values(self.values, forbid_id=True))


@dataclass(frozen=True)
class SettingsUpdateOperation:
    target_setting_id: int | None
    key: str
    value: str

    def __post_init__(self) -> None:
        if self.target_setting_id is not None:
            _positive_id(self.target_setting_id, "target_setting_id")
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Setting key must be non-empty")
        if not isinstance(self.value, str):
            raise ValueError("Setting value must be a string")


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

    def __post_init__(self) -> None:
        mapping = tuple((int(source), int(target)) for source, target in self.inbound_mapping)
        if len({source for source, _ in mapping}) != len(mapping):
            raise ValueError("Inbound mapping source IDs must be unique")
        for source, target in mapping:
            _positive_id(source, "source inbound id")
            _positive_id(target, "target inbound id")
        if any(operation.target_inbound_id == 1 for operation in self.inbound_deletes):
            raise ValueError("Inbound 1 cannot be deleted")
        for identifier in (*self.client_deletes, *self.traffic_deletes):
            _positive_id(identifier, "delete id")
        if len(set(self.client_deletes)) != len(self.client_deletes):
            raise ValueError("Client delete IDs must be unique")
        object.__setattr__(self, "inbound_mapping", mapping)
        object.__setattr__(self, "validation_errors", tuple(self.validation_errors))

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
