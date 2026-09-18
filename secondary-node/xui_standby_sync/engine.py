"""Atomic executor for validated DatabaseSyncPlan objects."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from .database import SQL_IDENTIFIER_RE
from .exceptions import OperationCancelledError, PlanExecutionError, PlanValidationError
from .models import DatabaseFingerprint, DatabaseSyncPlan

_HOST_INVARIANTS = {"webPort", "subPort", "subURI", "tgBotEnable"}


def _ensure_not_cancelled(is_cancelled: Callable[[], bool] | None) -> None:
    if is_cancelled is not None and is_cancelled():
        raise OperationCancelledError("Database plan execution was cancelled")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not SQL_IDENTIFIER_RE.fullmatch(table):
        raise PlanExecutionError(f"Invalid table identifier: {table!r}")
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table});")}


def _host_invariant_snapshot(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT key, value FROM settings;").fetchall()
    return {
        str(row["key"]): str(row["value"])
        for row in rows
        if row["key"] in _HOST_INVARIANTS
        or str(row["key"]).endswith(("CertFile", "KeyFile"))
    }


def _assert_inbound_one_protected(inbound_id: int) -> None:
    if inbound_id == 1:
        raise PlanExecutionError("Inbound 1 must not be deleted or overwritten")


def _assert_dual_layer(connection: sqlite3.Connection, inbound_ids: set[int]) -> None:
    for inbound_id in inbound_ids:
        row = connection.execute("SELECT settings FROM inbounds WHERE id = ?;", (inbound_id,)).fetchone()
        if row is None:
            raise PlanExecutionError(f"Inbound {inbound_id} disappeared during merge")
        try:
            clients_json = json.loads(row["settings"] or "{}")
        except (TypeError, json.JSONDecodeError) as error:
            raise PlanExecutionError(f"Inbound {inbound_id} has malformed settings JSON") from error
        clients = clients_json.get("clients") if isinstance(clients_json, dict) else None
        if not isinstance(clients, list):
            raise PlanExecutionError(f"Inbound {inbound_id} settings lacks clients array")
        relational = connection.execute(
            "SELECT uuid, email FROM clients WHERE inbound_id = ?;", (inbound_id,)
        ).fetchall()
        if len(clients) != len(relational):
            raise PlanExecutionError(f"Inbound {inbound_id} clients JSON is inconsistent with relational clients")
        identities = {(str(row["uuid"]), str(row["email"])) for row in relational}
        for client in clients:
            if not isinstance(client, dict):
                raise PlanExecutionError(f"Inbound {inbound_id} clients JSON contains non-object")
            if "uuid" in client and not any(str(client["uuid"]) == uuid for uuid, _ in identities):
                raise PlanExecutionError(f"Inbound {inbound_id} clients JSON references unknown UUID")
            if "email" in client and not any(str(client["email"]) == email for _, email in identities):
                raise PlanExecutionError(f"Inbound {inbound_id} clients JSON references unknown email")


def apply_database_sync_plan(
    *,
    target_db: Path,
    plan: DatabaseSyncPlan,
    is_cancelled: Callable[[], bool] | None = None,
) -> None:
    """Apply an already-built plan; no matching or planning decisions occur here."""
    if not plan.is_valid:
        raise PlanValidationError("Cannot apply invalid database plan")
    connection = sqlite3.connect(target_db, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.execute("PRAGMA busy_timeout = 5000;")
        connection.execute("BEGIN IMMEDIATE;")
        invariant_baseline = _host_invariant_snapshot(connection)
        touched_inbounds = {operation.target_inbound_id for operation in plan.inbound_client_updates}
        if _current_fingerprint(connection) != plan.target_fingerprint:
            raise PlanExecutionError(
                "Target database fingerprint changed since planning"
            )
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
            # Only the clients array may be updated for protected inbound 1.
            cursor = connection.execute(
                "UPDATE inbounds SET settings = ? WHERE id = ?;",
                (
                    inbound_settings_op.settings_json,
                    inbound_settings_op.target_inbound_id,
                ),
            )
            if cursor.rowcount == 0:
                raise PlanExecutionError(
                    f"Target inbound {inbound_settings_op.target_inbound_id} not found"
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
            cursor = connection.execute(
                f"UPDATE clients SET {assignments} WHERE id = ?;",
                (*values.values(), client_update_op.target_client_id),
            )
            if cursor.rowcount == 0:
                raise PlanExecutionError(
                    f"Target client {client_update_op.target_client_id} not found"
                )
            _ensure_not_cancelled(is_cancelled)

        for client_id in plan.client_deletes:
            cursor = connection.execute("DELETE FROM clients WHERE id = ?;", (client_id,))
            if cursor.rowcount == 0:
                raise PlanExecutionError(f"Target client {client_id} not found")
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
            cursor = connection.execute(
                f"UPDATE client_traffics SET {assignments} WHERE id = ?;",
                (*values.values(), traffic_update_op.target_traffic_id),
            )
            if cursor.rowcount == 0:
                raise PlanExecutionError(
                    f"Target client traffic {traffic_update_op.target_traffic_id} not found"
                )
            _ensure_not_cancelled(is_cancelled)

        for traffic_id in plan.traffic_deletes:
            cursor = connection.execute("DELETE FROM client_traffics WHERE id = ?;", (traffic_id,))
            if cursor.rowcount == 0:
                raise PlanExecutionError(f"Target client traffic {traffic_id} not found")
            _ensure_not_cancelled(is_cancelled)

        for inbound_delete_op in plan.inbound_deletes:
            _assert_inbound_one_protected(inbound_delete_op.target_inbound_id)
            connection.execute(
                "DELETE FROM clients WHERE inbound_id = ?;",
                (inbound_delete_op.target_inbound_id,),
            )
            connection.execute(
                "DELETE FROM client_traffics WHERE inbound_id = ?;",
                (inbound_delete_op.target_inbound_id,),
            )
            cursor = connection.execute(
                "DELETE FROM inbounds WHERE id = ?;",
                (inbound_delete_op.target_inbound_id,),
            )
            if cursor.rowcount == 0:
                raise PlanExecutionError(f"Target inbound {inbound_delete_op.target_inbound_id} not found")
            _ensure_not_cancelled(is_cancelled)

        for setting_op in plan.settings_updates:
            if setting_op.target_setting_id is None:
                connection.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?);",
                    (setting_op.key, setting_op.value),
                )
            else:
                cursor = connection.execute(
                    "UPDATE settings SET value = ? WHERE id = ?;",
                    (setting_op.value, setting_op.target_setting_id),
                )
                if cursor.rowcount == 0:
                    raise PlanExecutionError(f"Target setting {setting_op.target_setting_id} not found")
            _ensure_not_cancelled(is_cancelled)

        if plan.xray_template_json is not None:
            cursor = connection.execute(
                "UPDATE settings SET value = ? WHERE key = 'xrayTemplateConfig';",
                (plan.xray_template_json,),
            )
            if cursor.rowcount == 0:
                raise PlanExecutionError("xrayTemplateConfig setting not found")
        _assert_dual_layer(connection, touched_inbounds)
        permitted_setting_ids = {
            operation.target_setting_id for operation in plan.settings_updates
            if operation.target_setting_id is not None
        }
        current_invariants = _host_invariant_snapshot(connection)
        changed_invariants = {
            key for key, value in current_invariants.items()
            if invariant_baseline.get(key) != value
        }
        changed_setting_keys = {
            str(row["key"])
            for row in connection.execute(
                "SELECT key FROM settings WHERE id IN ({})".format(",".join("?" for _ in permitted_setting_ids)),
                tuple(permitted_setting_ids),
            )
        } if permitted_setting_ids else set()
        if changed_invariants - changed_setting_keys:
            raise PlanExecutionError("Host invariants changed during database merge")
        connection.execute("COMMIT;")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK;")
        raise
    finally:
        connection.close()


def _current_fingerprint(connection: sqlite3.Connection) -> DatabaseFingerprint:
    return DatabaseFingerprint(
        schema_version=int(connection.execute("PRAGMA schema_version;").fetchone()[0]),
        data_version=int(connection.execute("PRAGMA data_version;").fetchone()[0]),
    )
