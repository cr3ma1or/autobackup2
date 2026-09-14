"""Read-only SQLite integrity, schema, allowlist, and invariant helpers."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .exceptions import IntegrityCheckError, SchemaValidationError, SecurityViolationError
from .models import DatabaseFingerprint
from .security import verify_file_security

SQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_REQUIRED_TABLES = {"inbounds", "clients", "client_traffics", "settings"}


def connect_read_only(path: Path) -> sqlite3.Connection:
    verify_file_security(path, "SQLite database")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def verify_integrity(path: Path) -> None:
    if not path.exists():
        raise IntegrityCheckError(f"Cannot run integrity check on {path}: file does not exist")    
    try:
        with connect_read_only(path) as connection:
            result = connection.execute("PRAGMA integrity_check;").fetchone()
            if result is None or result[0] != "ok":
                raise IntegrityCheckError(f"SQLite integrity check failed for {path}: {result}")
    except (sqlite3.Error, SecurityViolationError, OSError) as error:
        if isinstance(error, IntegrityCheckError):
            raise
        raise IntegrityCheckError(f"SQLite integrity check failed for {path}: {error}") from error
    if result is None or result[0] != "ok":
        raise IntegrityCheckError(f"SQLite integrity check failed for {path}: {result}")


def get_database_fingerprint(connection: sqlite3.Connection) -> DatabaseFingerprint:
    try:
        schema_version = int(connection.execute("PRAGMA schema_version;").fetchone()[0])
        data_version = int(connection.execute("PRAGMA data_version;").fetchone()[0])
    except (TypeError, ValueError, sqlite3.Error) as error:
        raise IntegrityCheckError(
            f"Cannot read database fingerprint: {error}"
        ) from error
    return DatabaseFingerprint(schema_version, data_version)


def get_table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    if not SQL_IDENTIFIER_RE.fullmatch(table_name):
        raise SchemaValidationError(f"Invalid table identifier: {table_name!r}")
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name});")
    }
    if not columns:
        raise SchemaValidationError(f"Missing or empty table: {table_name}")
    return columns


def validate_allowlist(path: Path) -> dict[str, Any]:
    verify_file_security(path, "allowlist")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SchemaValidationError(
            f"Cannot parse allowlist {path}: {error}"
        ) from error
    if not isinstance(config, dict):
        raise SchemaValidationError("Allowlist root must be a JSON object")
    tables = config.get("tables")
    if not isinstance(tables, dict):
        raise SchemaValidationError("Allowlist tables must be an object")
    for table_name, table_config in tables.items():
        if not isinstance(table_name, str) or not SQL_IDENTIFIER_RE.fullmatch(
            table_name
        ):
            raise SchemaValidationError(
                f"Invalid allowlist table identifier: {table_name!r}"
            )
        if not isinstance(table_config, dict):
            raise SchemaValidationError(
                f"Allowlist table must be an object: {table_name}"
            )
        for key in ("matching_key", "fallback_matching_key"):
            if key in table_config:
                value = table_config[key]
                if not isinstance(value, str) or not SQL_IDENTIFIER_RE.fullmatch(value):
                    raise SchemaValidationError(f"Invalid {key} in {table_name}")
        for key in ("allowed_columns", "allowed_keys"):
            values = table_config.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(value, str) or not SQL_IDENTIFIER_RE.fullmatch(value)
                for value in values
            ):
                raise SchemaValidationError(f"Invalid {key} in {table_name}")
    missing_tables = _REQUIRED_TABLES - set(tables)
    if missing_tables:
        raise SchemaValidationError(
            f"Allowlist missing required tables: {sorted(missing_tables)}"
        )                
    invariants = config.get("assert_invariants", [])
    if not isinstance(invariants, (list, dict)):
        raise SchemaValidationError("assert_invariants must be a list or object")
    invariant_keys = invariants if isinstance(invariants, list) else invariants.keys()
    if any(
        not isinstance(key, str) or not SQL_IDENTIFIER_RE.fullmatch(key)
        for key in invariant_keys
    ):
        raise SchemaValidationError("Invalid assert_invariants key")
    return config


def validate_schema(
    source_db: Path, target_db: Path, allowlist: dict[str, Any]
) -> None:
    tables_to_check = set(allowlist.get("tables", {}).keys())
    with connect_read_only(source_db) as source, connect_read_only(target_db) as target:
        source_tables = {
            row[0]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        target_tables = {
            row[0]
            for row in target.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if missing := tables_to_check - source_tables:
            raise SchemaValidationError(f"Missing tables in source DB: {sorted(missing)}")
        if missing := tables_to_check - target_tables:
            raise SchemaValidationError(f"Missing tables in target DB: {sorted(missing)}")
        for table_name in tables_to_check:
            source_columns = get_table_columns(source, table_name)
            target_columns = get_table_columns(target, table_name)
            config = allowlist.get("tables", {}).get(table_name, {})
            required = {"id"} if table_name != "settings" else {"id", "key", "value"}
            if table_name == "inbounds":
                required |= {"port", "tag", "settings", "stream_settings"}
            elif table_name == "clients":
                required |= {
                    "inbound_id",
                    config.get("matching_key", "uuid"),
                    config.get("fallback_matching_key", "email"),
                }
            elif table_name == "client_traffics":
                required |= {"inbound_id", "email", config.get("matching_key", "email")}
            for label, columns in (
                ("source", source_columns),
                ("target", target_columns),
            ):
                if missing := required - columns:
                    raise SchemaValidationError(
                        f"{label} {table_name} missing columns: {sorted(missing)}"
                    )


def get_invariants_snapshot(path: Path, keys: list[str]) -> dict[str, str]:
    try:
        with connect_read_only(path) as connection:
            settings = dict(connection.execute("SELECT key, value FROM settings;"))
    except (sqlite3.Error, OSError) as error:
        raise IntegrityCheckError(f"Cannot read invariant snapshot: {error}") from error
    missing = [key for key in keys if key not in settings]
    if missing:
        raise IntegrityCheckError(
            f"Invariant keys missing from {path}: {sorted(missing)}"
        )
    return {key: str(settings[key]) for key in keys}


def verify_invariants(
    path: Path,
    invariants: list[str] | dict[str, Any],
    baseline: dict[str, str] | None = None,
) -> None:
    with connect_read_only(path) as connection:
        settings = dict(connection.execute("SELECT key, value FROM settings;"))
    if isinstance(invariants, list):
        if baseline is None:
            raise IntegrityCheckError("Invariant baseline is required")
        for key in invariants:
            if (
                key not in settings
                or key not in baseline
                or str(settings[key]) != baseline[key]
            ):
                raise IntegrityCheckError(f"Invariant changed or missing: {key}")
    else:
        for key, expected in invariants.items():
            if key not in settings or str(settings[key]) != str(expected):
                raise IntegrityCheckError(f"Invariant failed: {key}")
