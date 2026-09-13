"""Synchronization workflow state machine."""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .archive import decrypt_and_extract
from .backup import discover_backup
from .database import (
    get_invariants_snapshot,
    validate_allowlist,
    validate_schema,
    verify_integrity,
    verify_invariants,
)
from .engine import apply_database_sync_plan
from .exceptions import SyncError
from .locks import LockSet
from .models import BackupMetadata, ExecutionResult, RuntimeConfig, SyncRunPlan
from .planner import build_database_sync_plan
from .rollback import create_rollback_snapshot, restore_rollback_snapshot
from .security import verify_file_security
from .service import start_service, stop_service

logger = logging.getLogger(__name__)


def _plan_summary(plan: Any) -> dict[str, int]:
    database = plan.database if isinstance(plan, SyncRunPlan) else plan
    return {
        "inbounds_create": len(database.inbound_creates),
        "inbounds_delete": len(database.inbound_deletes),
        "clients_insert": len(database.client_inserts),
        "clients_update": len(database.client_updates),
        "clients_delete": len(database.client_deletes),
        "traffic_insert": len(database.traffic_inserts),
        "traffic_update": len(database.traffic_updates),
        "traffic_delete": len(database.traffic_deletes),
    }


def _result_payload(result: ExecutionResult) -> dict[str, Any]:
    payload = asdict(result)
    if result.plan is not None:
        payload["plan"] = {
            "backup": asdict(result.plan.backup),
            "summary": _plan_summary(result.plan),
        }
        payload["plan"]["backup"]["archive_path"] = str(result.plan.backup.archive_path)
    return payload


def _verify_standby(config: RuntimeConfig) -> None:
    marker = config.paths.standby_mode_file
    verify_file_security(marker, "standby marker")
    mode = marker.read_text(encoding="utf-8").strip()
    if mode != "STANDBY":
        raise SyncError(f"Synchronization is not allowed in standby mode: {mode}")
    failover_lock = config.paths.failover_lock_file
    if failover_lock.exists():
        raise SyncError("Failover lock is present")


def _build_plan(config: RuntimeConfig, source_db: Path):
    allowlist = validate_allowlist(config.paths.allowlist_path)
    validate_schema(source_db, config.paths.target_db, allowlist)
    return build_database_sync_plan(
        source_db=source_db,
        target_db=config.paths.target_db,
        allowlist=allowlist,
        reserved_ports=config.policy.custom_reserved_ports,
        primary_ip=config.policy.primary_ip,
        standby_ip=config.policy.standby_ip,
    ), allowlist


def run_sync(
    config: RuntimeConfig,
    *,
    backup_path: Path | None = None,
) -> ExecutionResult:
    """Run full sync lifecycle; all service/snapshot/engine orchestration lives here."""
    run_id = "preview"
    service_stopped = False
    service_restarted = False
    rollback_attempted = False
    rollback_succeeded: bool | None = None
    plan = None
    backup: BackupMetadata | None = None
    snapshot = None
    try:
        _verify_standby(config)
        with LockSet(
            config.paths.sync_lock_path,
            config.paths.store_lock_path,
        ):
            verify_file_security(config.paths.target_db, "target database")
            verify_integrity(config.paths.target_db)
            invariant_config = validate_allowlist(config.paths.allowlist_path)
            invariant_keys = invariant_config.get("assert_invariants", [])
            baseline = (
                get_invariants_snapshot(config.paths.target_db, invariant_keys)
                if isinstance(invariant_keys, list)
                else None
            )
            backup = discover_backup(
                incoming_dir=config.paths.incoming_dir,
                override_path=backup_path,
                allow_unsafe_path=config.security.allow_unsafe_backup_path,
                max_age_seconds=config.policy.max_age_seconds,
                max_clock_skew_seconds=config.policy.max_clock_skew_seconds,
            )
            if not backup.is_fresh and not config.options.force:
                raise SyncError("Backup is stale; use --force to override freshness")
            with tempfile.TemporaryDirectory(
                prefix="xui-standby-", dir=config.paths.work_root
            ) as work_dir_name:
                source_db, signer = decrypt_and_extract(
                    archive_path=backup.archive_path,
                    work_dir=Path(work_dir_name),
                    gnupg_dir=config.paths.gnupg_dir,
                    security=config.security,
                    timeout=config.policy.command_timeout,
                    terminate_gpg_agent=not config.options.dry_run,
                )
                backup = replace(
                    backup,
                    signer_fingerprint=signer or None,
                    signature_verified=bool(signer),
                )
                database_plan, _allowlist = _build_plan(config, source_db)
                plan = SyncRunPlan(backup=backup, database=database_plan)
                if config.options.dry_run:
                    return ExecutionResult(
                        True, run_id, plan, False, False, False, None, None, 0
                    )
                stop_service(config.policy.service_name, config.policy.command_timeout)
                service_stopped = True
                snapshot = create_rollback_snapshot(
                    target_db=config.paths.target_db,
                    snapshots_dir=config.paths.snapshots_dir,
                    backup=backup,
                    max_rollback_copies=config.policy.max_rollback_copies,
                )
                authoritative_plan, _ = _build_plan(config, source_db)
                plan = SyncRunPlan(backup=backup, database=authoritative_plan)
                apply_database_sync_plan(
                    target_db=config.paths.target_db,
                    plan=authoritative_plan,
                )
                verify_integrity(config.paths.target_db)
                if isinstance(invariant_keys, list):
                    verify_invariants(config.paths.target_db, invariant_keys, baseline)
                elif isinstance(invariant_keys, dict):
                    verify_invariants(config.paths.target_db, invariant_keys)
                start_service(config.policy.service_name, config.policy.command_timeout)
                service_restarted = True
        return ExecutionResult(
            True,
            snapshot.run_id if snapshot else run_id,
            plan,
            service_stopped,
            service_restarted,
            rollback_attempted,
            rollback_succeeded,
            None,
            0,
        )
    except Exception as error:
        logger.exception("Synchronization workflow failed")
        if service_stopped and snapshot is not None:
            rollback_attempted = True
            try:
                restore_rollback_snapshot(
                    target_db=config.paths.target_db,
                    snapshot_path=snapshot.snapshot_path,
                )
                rollback_succeeded = True
                start_service(config.policy.service_name, config.policy.command_timeout)
                service_restarted = True
            except SyncError:
                rollback_succeeded = False
        return ExecutionResult(
            False,
            snapshot.run_id if snapshot else run_id,
            plan,
            service_stopped,
            service_restarted,
            rollback_attempted,
            rollback_succeeded,
            str(error),
            1,
        )


def result_json(result: ExecutionResult) -> str:
    return json.dumps(_result_payload(result), ensure_ascii=True, indent=2, default=str)
