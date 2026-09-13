"""Atomic executor for validated DatabaseSyncPlan objects."""

from __future__ import annotations

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
        for inbound_create_op in plan.inbound_creates:
            values = {
                key: value
                for key, value in inbound_create_op.values.items()
                if key in inbound_columns and key != "id"
            }
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            cursor = connection.execute(
                f"INSERT INTO inbounds ({columns}) VALUES ({placeholders});",
                tuple(values.values()),
            )
            if cursor.lastrowid is None:
                raise PlanExecutionError(
                    "Failed to retrieve last inserted row ID for inbound"
                )
            inbound_mapping[inbound_create_op.source_inbound_id] = cursor.lastrowid
            _ensure_not_cancelled(is_cancelled)

        for inbound_settings_op in plan.inbound_client_updates:
            connection.execute(
                "UPDATE inbounds SET settings = ? WHERE id = ?;",
                (
                    inbound_settings_op.settings_json,
                    inbound_settings_op.target_inbound_id,
                ),
            )
            _ensure_not_cancelled(is_cancelled)

        client_columns = _columns(connection, "clients")
        for client_insert_op in plan.client_inserts:
            values = dict(client_insert_op.values)
            values["inbound_id"] = inbound_mapping[client_insert_op.source_inbound_id]
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

        for client_update_op in plan.client_updates:
            values = dict(client_update_op.values)
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE clients SET {assignments} WHERE id = ?;",
                (*values.values(), client_update_op.target_client_id),
            )
            _ensure_not_cancelled(is_cancelled)

        for client_id in plan.client_deletes:
            connection.execute("DELETE FROM clients WHERE id = ?;", (client_id,))
            _ensure_not_cancelled(is_cancelled)

        traffic_columns = _columns(connection, "client_traffics")
        for traffic_insert_op in plan.traffic_inserts:
            values = dict(traffic_insert_op.values)
            values["inbound_id"] = inbound_mapping[traffic_insert_op.source_inbound_id]
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

        for traffic_update_op in plan.traffic_updates:
            values = dict(traffic_update_op.values)
            assignments = ", ".join(f"{key} = ?" for key in values)
            connection.execute(
                f"UPDATE client_traffics SET {assignments} WHERE id = ?;",
                (*values.values(), traffic_update_op.target_traffic_id),
            )
            _ensure_not_cancelled(is_cancelled)

        for traffic_id in plan.traffic_deletes:
            connection.execute(
                "DELETE FROM client_traffics WHERE id = ?;", (traffic_id,)
            )
            _ensure_not_cancelled(is_cancelled)

        for inbound_delete_op in plan.inbound_deletes:
            connection.execute(
                "DELETE FROM clients WHERE inbound_id = ?;",
                (inbound_delete_op.target_inbound_id,),
            )
            connection.execute(
                "DELETE FROM client_traffics WHERE inbound_id = ?;",
                (inbound_delete_op.target_inbound_id,),
            )
            connection.execute(
                "DELETE FROM inbounds WHERE id = ?;",
                (inbound_delete_op.target_inbound_id,),
            )
            _ensure_not_cancelled(is_cancelled)

        for setting_op in plan.settings_updates:
            if setting_op.target_setting_id is None:
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?);",
                    (setting_op.key, setting_op.value),
                )
            else:
                connection.execute(
                    "UPDATE settings SET value = ? WHERE id = ?;",
                    (setting_op.value, setting_op.target_setting_id),
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


def _current_fingerprint(connection: sqlite3.Connection) -> DatabaseFingerprint:
    return DatabaseFingerprint(
        schema_version=int(connection.execute("PRAGMA schema_version;").fetchone()[0]),
        data_version=int(connection.execute("PRAGMA data_version;").fetchone()[0]),
    )
