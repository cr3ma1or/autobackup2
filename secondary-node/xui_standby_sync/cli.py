"""Command-line interface, including the legacy sync alias."""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any

from .backup import discover_backup
from .commands import run_command  # noqa: F401 — legacy patch point for callers
from .config import build_runtime_config, load_values
from .database import validate_allowlist, verify_integrity
from .exceptions import SyncError
from .locks import LockSet
from .models import RuntimeConfig
from .rollback import restore_rollback_snapshot
from .security import verify_file_security
from .service import start_service, stop_service
from .status import collect_status, status_json
from .workflow import result_json, run_sync

_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f-]+$")
_SIGNAL_EXIT_CODES: dict[int, int] = {
    signal.SIGINT: 130,
    signal.SIGTERM: 143,
}


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
    rollback_target = rollback.add_mutually_exclusive_group(required=True)
    rollback_target.add_argument("--snapshot", type=Path)
    rollback_target.add_argument("--run-id", type=str)
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


def _install_signal_handlers() -> tuple[
    dict[int, Any], Callable[[], bool], Callable[[], int | None]
]:
    received: int | None = None

    def handler(signum: int, _frame: FrameType | None) -> None:
        nonlocal received
        received = signum

    previous: dict[int, Any] = {}
    for signum in _SIGNAL_EXIT_CODES:
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous, lambda: received is not None, lambda: received


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    previous_handlers, is_cancelled, received_signal = _install_signal_handlers()
    exit_code = 1
    try:
        config = _runtime_config(args)
        if args.command == "status":
            if args.json:
                output = status_json(
                    config.paths, service_name=config.policy.service_name
                )
            else:
                status_data = collect_status(
                    config.paths, service_name=config.policy.service_name
                )
                output = "\n".join(f"{k}: {v}" for k, v in status_data.items())
            print(output)
            exit_code = 0
        elif args.command in {"sync", "plan"}:
            result = run_sync(
                config, backup_path=args.backup, is_cancelled=is_cancelled
            )
            print(
                result_json(result) if args.json else result.error_message or "success"
            )
            exit_code = result.exit_code
        elif args.command == "validate":
            verify_integrity(config.paths.target_db)
            validate_allowlist(config.paths.allowlist_path)
            validation_payload: dict[str, Any] = {"valid": True}
            print(json.dumps(validation_payload) if args.json else "valid")
            exit_code = 0
        elif args.command == "check-backup":
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
            exit_code = 0 if metadata.is_fresh or config.options.force else 1
        elif args.command == "rollback":
            snapshot_path = args.snapshot
            run_id = getattr(args, "run_id", None)
            if snapshot_path is None:
                if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
                    raise SyncError("Invalid rollback run-id")
                snapshot_path = (
                    config.paths.snapshots_dir
                    / run_id
                    / "target-before-sync.db"
                )
            verify_file_security(
                config.paths.standby_mode_file, "standby marker"
            )
            if (
                config.paths.standby_mode_file.read_text(encoding="utf-8").strip()
                != "STANDBY"
            ):
                raise SyncError("Rollback requires STANDBY mode")
            with LockSet(
                config.paths.sync_lock_path,
                config.paths.store_lock_path,
            ):
                lifecycle_error: BaseException | None = None
                try:
                    stop_service(
                        config.policy.service_name, config.policy.command_timeout
                    )
                    restore_rollback_snapshot(
                        target_db=config.paths.target_db,
                        snapshot_path=snapshot_path,
                    )
                except BaseException as error:
                    lifecycle_error = error
                finally:
                    try:
                        start_service(
                            config.policy.service_name,
                            config.policy.command_timeout,
                        )
                    except BaseException as start_error:
                        if lifecycle_error is None:
                            lifecycle_error = start_error
                        else:
                            lifecycle_error = SyncError(
                                f"{lifecycle_error}; service restart failed: "
                                f"{start_error}"
                            )
                if lifecycle_error is not None:
                    raise lifecycle_error
            rollback_payload: dict[str, Any] = {
                "success": True,
                "snapshot": str(snapshot_path),
                "run_id": run_id,
            }
            print(json.dumps(rollback_payload) if args.json else "rollback complete")
            exit_code = 0
        else:
            raise SyncError(f"Unsupported command: {args.command}")
    except KeyboardInterrupt:
        exit_code = 130
    except Exception as error:
        logging.getLogger(__name__).error("Command failed: %s", error)
        if args.json:
            print(json.dumps({"success": False, "error": str(error)}))
        exit_code = 1
    finally:
        _restore_signal_handlers(previous_handlers)

    signum = received_signal()
    if isinstance(signum, int):
        return _SIGNAL_EXIT_CODES.get(signum, exit_code)
    return exit_code


def legacy_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xui-standby-sync")
    _add_sync_options(parser)
    args = parser.parse_args(argv)
    args.command = "sync"
    args.plan_only = False
    return main(["sync", *(argv or [])])
