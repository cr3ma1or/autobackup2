"""Command-line interface, including the legacy sync alias."""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from .backup import discover_backup
from .config import build_runtime_config, load_values
from .database import validate_allowlist, verify_integrity
from .exceptions import SyncError
from .locks import LockSet
from .models import RuntimeConfig
from .rollback import restore_rollback_snapshot
from .service import start_service, stop_service
from .status import status_json
from .workflow import result_json, run_sync


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xui-standby")
    subparsers = parser.add_subparsers(dest="command", required=True)
    sync = subparsers.add_parser("sync")
    _add_sync_options(sync)
    plan = subparsers.add_parser("plan")
    _add_sync_options(plan)
    plan.set_defaults(plan_only=True)
    validate = subparsers.add_parser("validate")
    _add_common_options(validate)
    check_backup = subparsers.add_parser("check-backup")
    _add_common_options(check_backup)
    check_backup.add_argument("--backup", type=Path, required=True)
    check_backup.add_argument("--unsafe-backup-path", action="store_true")
    rollback = subparsers.add_parser("rollback")
    _add_common_options(rollback)
    rollback.add_argument("--snapshot", type=Path)
    rollback.add_argument("--yes", action="store_true", required=True)
    status = subparsers.add_parser("status")
    _add_common_options(status)
    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--allowlist", type=Path)
    parser.add_argument("--timeout", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")


def _add_sync_options(parser: argparse.ArgumentParser) -> None:
    _add_common_options(parser)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--unsafe-backup-path", action="store_true")


def _runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    values = load_values(getattr(args, "config", None))
    overrides = {}
    if args.timeout is not None:
        overrides["CMD_TIMEOUT"] = str(args.timeout)
    config = build_runtime_config(
        values,
        force=getattr(args, "force", False),
        dry_run=getattr(args, "dry_run", False) or getattr(args, "plan_only", False),
        json_output=getattr(args, "json", False),
        verbose=getattr(args, "verbose", False),
        unsafe_backup_path=getattr(args, "unsafe_backup_path", False),
        cli_overrides=overrides,
    )
    if getattr(args, "allowlist", None) is not None:
        config = replace(
            config,
            paths=replace(config.paths, allowlist_path=args.allowlist),
        )
    return config


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "status":
        config = _runtime_config(args)
        output = status_json(config.paths, service_name=config.policy.service_name)
        print(output)
        return 0
    try:
        config = _runtime_config(args)
        if args.command in {"sync", "plan"}:
            result = run_sync(config, backup_path=args.backup)
            print(
                result_json(result) if args.json else result.error_message or "success"
            )
            return result.exit_code
        if args.command == "validate":
            verify_integrity(config.paths.target_db)
            validate_allowlist(config.paths.allowlist_path)
            validation_payload: dict[str, Any] = {"valid": True}
            print(json.dumps(validation_payload) if args.json else "valid")
            return 0
        if args.command == "check-backup":
            metadata = discover_backup(
                incoming_dir=config.paths.incoming_dir,
                override_path=args.backup,
                allow_unsafe_path=config.security.allow_unsafe_backup_path,
                max_age_seconds=config.policy.max_age_seconds,
                max_clock_skew_seconds=config.policy.max_clock_skew_seconds,
            )
            backup_payload: dict[str, Any] = {
                "valid": True,
                "backup": metadata.__dict__,
            }
            print(
                json.dumps(backup_payload, default=str)
                if args.json
                else metadata.archive_name
            )
            return 0 if metadata.is_fresh or config.options.force else 1
        if args.command == "rollback":
            if args.snapshot is None:
                raise SyncError("--snapshot is required for rollback")
            if (
                config.paths.standby_mode_file.read_text(encoding="utf-8").strip()
                != "STANDBY"
            ):
                raise SyncError("Rollback requires STANDBY mode")
            with LockSet(
                config.paths.sync_lock_path,
                config.paths.store_lock_path,
            ):
                stop_service(config.policy.service_name, config.policy.command_timeout)
                restore_rollback_snapshot(
                    target_db=config.paths.target_db,
                    snapshot_path=args.snapshot,
                )
                start_service(config.policy.service_name, config.policy.command_timeout)
            rollback_payload: dict[str, Any] = {
                "success": True,
                "snapshot": str(args.snapshot),
            }
            print(json.dumps(rollback_payload) if args.json else "rollback complete")
            return 0
        raise SyncError(f"Unsupported command: {args.command}")
    except (SyncError, OSError) as error:
        logging.getLogger(__name__).error("Command failed: %s", error)
        if args.json:
            print(json.dumps({"success": False, "error": str(error)}))
        return 1


def legacy_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xui-standby-sync")
    _add_sync_options(parser)
    args = parser.parse_args(argv)
    args.command = "sync"
    args.plan_only = False
    return main(["sync", *(argv or [])])
