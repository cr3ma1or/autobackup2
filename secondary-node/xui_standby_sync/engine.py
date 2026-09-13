"""Atomic executor for validated DatabaseSyncPlan objects."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .exceptions import OperationCancelledError, PlanExecutionError, PlanValidationError
from .models import DatabaseFingerprint, DatabaseSyncPlan


def _ensure_not_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise OperationCancelledError("Database plan execution was cancelled")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table});")}


def apply_database_sync_plan(
    *,
    target_db: Path,
    plan: DatabaseSyncPlan,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    """Apply an already-built plan; no matching or planning decisions occur here."""
    if not plan.is_valid:
        raise PlanValidationError("Cannot apply invalid database plan")
    # CRITICAL FIX D4: Use default isolation_level for proper transaction management
    # isolation_level=None causes autocommit mode, breaking ROLLBACK semantics
    connection = sqlite3.connect(target_db)
    connection.row_factory = sqlite3.Row
    try:
        if _current_fingerprint(connection) != plan.target_fingerprint:
            raise PlanExecutionError(
                "Target database fingerprint changed since planning"
            )
        # sqlite3 will automatically begin transaction on first DML statement
        # Explicit BEGIN is not needed with default isolation_level
        _ensure_not_cancelled(is_cancelled)
        inbound_mapping = dict(plan.inbound_mapping)
        inbound_columns = _columns(connection, "inbounds")
        for operation in plan.inbound_creates:
            values = {
                key: value
                for key, value in operation.values.items()
                if key in inbound_columns and key != "id"
            }
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            cursor = connection.execute(
                f"INSERT INTO inbounds ({columns}) VALUES ({placeholders});",
                tuple(values.values()),
            )
            inbound_mapping[operation.source_inbound_id] = int(cursor.lastrowid)
            _ensure_not_cancelled(is_cancelled)

        for operation in plan.inbound_client_updates:
            connection.execute(
                "UPDATE inbounds SET settings = ? WHERE id = ?;",
                (operation.settings_json, operation.target_inbound_id),
            )
            _ensure_not_cancelled(is_cancelled)

        client_columns = _columns(connection, "clients")
        for operation in plan.client_inserts:
            values = dict(operation.values)
            values["inbound_id"] = inbound_mapping[operation.source_inbound_id]
            values = {
                key: value for key, value in values.items() if key in client_columns
            }
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO clients ({columns}) VALUES ({placeholders});",
                tuple(values.values()),
            )
            _ensure_not_cancelled(is_cancelled)

        for operation in plan.client_updates:
            values = dict(operation.values)
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE clients SET {assignments} WHERE id = ?;",
                (*values.values(), operation.target_client_id),
            )
            _ensure_not_cancelled(is_cancelled)

        for client_id in plan.client_deletes:
            connection.execute("DELETE FROM clients WHERE id = ?;", (client_id,))
            _ensure_not_cancelled(is_cancelled)

        traffic_columns = _columns(connection, "client_traffics")
        for operation in plan.traffic_inserts:
            values = dict(operation.values)
            values["inbound_id"] = inbound_mapping[operation.source_inbound_id]
            values = {
                key: value for key, value in values.items() if key in traffic_columns
            }
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            connection.execute(
                f"INSERT INTO client_traffics ({columns}) VALUES ({placeholders});",
                tuple(values.values()),
            )
            _ensure_not_cancelled(is_cancelled)

        for operation in plan.traffic_updates:
            values = dict(operation.values)
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE client_traffics SET {assignments} WHERE id = ?;",
                (*values.values(), operation.target_traffic_id),
            )
            _ensure_not_cancelled(is_cancelled)

        for traffic_id in plan.traffic_deletes:
            connection.execute(
                "DELETE FROM client_traffics WHERE id = ?;", (traffic_id,)
            )
            _ensure_not_cancelled(is_cancelled)

        for operation in plan.inbound_deletes:
            connection.execute(
                "DELETE FROM clients WHERE inbound_id = ?;",
                (operation.target_inbound_id,),
            )
            connection.execute(
                "DELETE FROM client_traffics WHERE inbound_id = ?;",
                (operation.target_inbound_id,),
            )
            connection.execute(
                "DELETE FROM inbounds WHERE id = ?;", (operation.target_inbound_id,)
            )
            _ensure_not_cancelled(is_cancelled)

        for operation in plan.settings_updates:
            if operation.target_setting_id is None:
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?);",
                    (operation.key, operation.value),
                )
            else:
                connection.execute(
                    "UPDATE settings SET value = ? WHERE id = ?;",
                    (operation.value, operation.target_setting_id),
                )
            _ensure_not_cancelled(is_cancelled)

        if plan.xray_template_json is not None:
            connection.execute(
                "UPDATE settings SET value = ? WHERE key = 'xrayTemplateConfig';",
                (plan.xray_template_json,),
            )
        connection.commit()
    except BaseException:
        # sqlite3 will automatically rollback on exception with default isolation_level
        connection.rollback()
        raise
    finally:
        connection.close()


def _current_fingerprint(connection: sqlite3.Connection):
    return DatabaseFingerprint(
        schema_version=int(connection.execute("PRAGMA schema_version;").fetchone()[0]),
        data_version=int(connection.execute("PRAGMA data_version;").fetchone()[0]),
    )
