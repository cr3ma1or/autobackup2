"""Read-only deterministic database synchronization planner."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .database import (
    connect_read_only,
    get_database_fingerprint,
    validate_schema,
)
from .exceptions import PlanValidationError

_HOST_SETTING_DENYLIST = {"webPort", "subPort", "subURI", "tgBotEnable"}


def _is_host_setting(key: str) -> bool:
    return key in _HOST_SETTING_DENYLIST or key.endswith("CertFile") or key.endswith("KeyFile")


def _validate_unique_client_keys(rows: list[sqlite3.Row], matching_key: str, fallback_key: str, label: str) -> None:
    seen_matching: set[str] = set()
    seen_fallback: set[str] = set()
    for row in rows:
        matching = row[matching_key]
        fallback = row[fallback_key]
        if matching:
            if str(matching) in seen_matching:
                raise PlanValidationError(f"Duplicate {matching_key} in {label}: {matching}")
            seen_matching.add(str(matching))
        if fallback:
            if str(fallback) in seen_fallback:
                raise PlanValidationError(f"Duplicate {fallback_key} in {label}: {fallback}")
            seen_fallback.add(str(fallback))


def _normalised_clients_settings(row: sqlite3.Row, clients: list[sqlite3.Row], label: str) -> str:
    settings = _json_object(row["settings"], label)
    if clients:
        row_columns = set(clients[0].keys())
        settings["clients"] = [
            {key: client[key] for key in row_columns if key not in {"id", "inbound_id"}}
            for client in clients
        ]
    return _settings_json(settings, label)
from .models import (
    ClientInsertOperation,
    ClientUpdateOperation,
    DatabaseSyncPlan,
    InboundClientSettingsUpdateOperation,
    InboundCreateOperation,
    InboundDeleteOperation,
    SettingsUpdateOperation,
    TrafficInsertOperation,
    TrafficUpdateOperation,
)


def _mapping(values: dict[str, Any]) -> MappingProxyType[str, Any]:
    return MappingProxyType(dict(values))


def _json_object(value: Any, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError) as error:
        raise PlanValidationError(f"Malformed JSON in {label}") from error
    if not isinstance(parsed, dict):
        raise PlanValidationError(f"{label} must be a JSON object")
    return parsed


def _settings_json(settings: dict[str, Any], label: str) -> str:
    if not isinstance(settings.get("clients"), list):
        raise PlanValidationError(f"{label}.clients must be a JSON array")
    return json.dumps(settings, ensure_ascii=False, separators=(",", ":"))


def _stream_settings(
    row: sqlite3.Row,
    primary_ip: str | None,
    standby_ip: str | None,
    allowed_columns: frozenset[str] | None = None,
) -> dict[str, Any]:
    row_columns = set(row.keys())
    if allowed_columns:
        values = {key: row[key] for key in allowed_columns & row_columns if key != "id"}
    else:
        values = {key: row[key] for key in row_columns if key != "id"}
    if primary_ip and standby_ip and isinstance(values.get("stream_settings"), str):
        values["stream_settings"] = values["stream_settings"].replace(
            primary_ip, standby_ip
        )
    return values


def build_database_sync_plan(
    *,
    source_db: Path,
    target_db: Path,
    allowlist: dict[str, Any],
    reserved_ports: frozenset[int],
    primary_ip: str | None,
    standby_ip: str | None,
) -> DatabaseSyncPlan:
    """Build a plan using only read-only source and target database connections."""
    validate_schema(source_db, target_db, allowlist)
    with connect_read_only(source_db) as source, connect_read_only(target_db) as target:
        fingerprint = get_database_fingerprint(target)
        source_clients = source.execute("SELECT * FROM clients ORDER BY id;").fetchall()
        target_clients = target.execute("SELECT * FROM clients ORDER BY id;").fetchall()
        source_clients_by_inbound: dict[int, list[sqlite3.Row]] = {}
        for client in source_clients:
            source_clients_by_inbound.setdefault(int(client["inbound_id"]), []).append(client)
        source_primary = source.execute(
            "SELECT * FROM inbounds WHERE id = 1;"
        ).fetchone()
        target_primary = target.execute(
            "SELECT * FROM inbounds WHERE id = 1;"
        ).fetchone()
        if source_primary is None or target_primary is None:
            raise PlanValidationError(
                "Inbound id=1 is required in source and target DB"
            )

        source_inbounds = source.execute(
            "SELECT * FROM inbounds ORDER BY id;"
        ).fetchall()
        target_inbounds = target.execute(
            "SELECT * FROM inbounds ORDER BY id;"
        ).fetchall()
        target_secondary = [row for row in target_inbounds if row["id"] != 1]
        reserved = set(reserved_ports)
        reserved.add(int(target_primary["port"]))
        mapping: dict[int, int] = {int(source_primary["id"]): int(target_primary["id"])}
        creates: list[InboundCreateOperation] = []
        inbound_updates: list[InboundClientSettingsUpdateOperation] = []
        deletes: list[InboundDeleteOperation] = []
        source_secondary = [row for row in source_inbounds if row["id"] != 1]
        planned_source_ids = {int(row["id"]) for row in source_secondary}
        matched_target_ids: set[int] = set()
        inbounds_config = allowlist.get("tables", {}).get("inbounds", {})
        inbounds_allowed = frozenset(
            inbounds_config.get("allowed_columns", [])
        ) | frozenset({"port", "tag", "settings", "stream_settings"})

        source_clients_json = _normalised_clients_settings(
            source_primary, source_clients_by_inbound.get(1, []), "source inbound 1 settings"
        )
        target_clients_json = _settings_json(
            _json_object(target_primary["settings"], "target inbound 1 settings"),
            "target inbound 1 settings",
        )
        if source_clients_json != target_clients_json:
            inbound_updates.append(
                InboundClientSettingsUpdateOperation(
                    int(target_primary["id"]), source_clients_json
                )
            )

        for source_row in source_secondary:
            source_tag = source_row["tag"]
            source_port = source_row["port"]
            if source_port is None:
                raise PlanValidationError(
                    f"Source inbound {source_row['id']} has no port"
                )
            matches = [
                row
                for row in target_secondary
                if row["tag"] == source_tag or row["port"] == source_port
            ]
            if len({int(row["id"]) for row in matches}) > 1:
                raise PlanValidationError(
                    f"Ambiguous mapping for inbound id={source_row['id']}"
                )
            if matches:
                target_row = matches[0]
                if target_row["port"] != source_port or (
                    source_tag and target_row["tag"] and target_row["tag"] != source_tag
                ):
                    raise PlanValidationError(
                        f"Inbound collision for source id={source_row['id']}"
                    )
                mapping[int(source_row["id"])] = int(target_row["id"])
                matched_target_ids.add(int(target_row["id"]))
                source_clients_json = _normalised_clients_settings(
                    source_row, source_clients_by_inbound.get(int(source_row["id"]), []),
                    "source secondary inbound",
                )
                target_clients_json = _settings_json(
                    _json_object(target_row["settings"], f"target inbound {target_row['id']} settings"),
                    "target secondary inbound",
                )
                if source_clients_json != target_clients_json:
                    inbound_updates.append(
                        InboundClientSettingsUpdateOperation(
                            int(target_row["id"]), source_clients_json
                        )
                    )
            else:
                occupied = {
                    int(row["port"])
                    for row in target_inbounds
                    if row["port"] is not None
                }
                if int(source_port) in reserved or int(source_port) in occupied:
                    raise PlanValidationError(
                        f"Cannot import inbound {source_row['id']}: port "
                        f"{source_port} is occupied or reserved"
                    )
                create_values = _stream_settings(source_row, primary_ip, standby_ip, inbounds_allowed)
                create_values["settings"] = _normalised_clients_settings(
                    source_row, source_clients_by_inbound.get(int(source_row["id"]), []),
                    f"source inbound {source_row['id']} settings",
                )
                creates.append(InboundCreateOperation(int(source_row["id"]), _mapping(create_values)))

        source_tags = {row["tag"] for row in source_secondary if row["tag"]}
        source_ports = {row["port"] for row in source_secondary}
        for target_row in target_secondary:
            target_id = int(target_row["id"])
            if int(target_row["port"]) in reserved:
                continue
            if (
                target_id not in matched_target_ids
                and target_row["tag"] not in source_tags
                and target_row["port"] not in source_ports
            ):
                deletes.append(
                    InboundDeleteOperation(
                        target_id, int(target_row["port"]), target_row["tag"]
                    )
                )

        client_config = allowlist["tables"]["clients"]
        matching_key = client_config["matching_key"]
        fallback_key = client_config.get("fallback_matching_key", "email")
        client_columns = set(client_config["allowed_columns"])
        _validate_unique_client_keys(source_clients, matching_key, fallback_key, "source clients")
        _validate_unique_client_keys(target_clients, matching_key, fallback_key, "target clients")
        target_by_key = {
            (row[matching_key] if row[matching_key] else row[fallback_key]): row
            for row in target_clients
            if row[matching_key] or row[fallback_key]
        }
        active_matching_keys: set[str] = set()
        active_fallback_keys: set[str] = set()
        client_inserts: list[ClientInsertOperation] = []
        client_updates: list[ClientUpdateOperation] = []
        for row in source_clients:
            row_columns = set(row.keys())
            match_value = row[matching_key] or row[fallback_key]
            if not match_value:
                raise PlanValidationError(f"Client {row['id']} has no matching key")
            if row[matching_key]:
                active_matching_keys.add(str(row[matching_key]))
            if row[fallback_key]:
                active_fallback_keys.add(str(row[fallback_key]))
            values = {
                column: row[column]
                for column in client_columns
                if column in row_columns and column != "id"
            }
            if (
                row["inbound_id"] not in mapping
                and row["inbound_id"] not in planned_source_ids
            ):
                raise PlanValidationError(
                    f"Client {row['id']} references unmapped inbound"
                )
            if row["inbound_id"] in mapping:
                values["inbound_id"] = mapping[row["inbound_id"]]
            else:
                values["inbound_id"] = row["inbound_id"]
            existing = target_by_key.get(match_value)
            if existing is None:
                client_inserts.append(
                    ClientInsertOperation(int(row["inbound_id"]), _mapping(values))
                )
            else:
                changed = {
                    key: value
                    for key, value in values.items()
                    if existing[key] != value
                }
                if changed:
                    client_updates.append(
                        ClientUpdateOperation(int(existing["id"]), _mapping(changed))
                    )

        managed_target_ids = set(mapping.values())
        client_deletes = tuple(
            int(row["id"])
            for row in target_clients
            if row["inbound_id"] in managed_target_ids
            and (
                str(row[matching_key]) not in active_matching_keys
                and str(row[fallback_key]) not in active_fallback_keys
            )
        )

        managed_target_count = sum(1 for row in target_clients if row["inbound_id"] in managed_target_ids)
        if managed_target_count > 1 and len(client_deletes) * 2 > managed_target_count:
            raise PlanValidationError("CRITICAL: mass client deletion exceeds 50% of Standby clients")

        traffic_config = allowlist["tables"]["client_traffics"]
        traffic_key = traffic_config["matching_key"]
        traffic_columns = set(traffic_config["allowed_columns"])
        source_traffic = source.execute("SELECT * FROM client_traffics;").fetchall()
        target_traffic = target.execute("SELECT * FROM client_traffics;").fetchall()
        target_traffic_by_key = {
            (row[traffic_key], row["inbound_id"]): row for row in target_traffic
        }
        traffic_inserts: list[TrafficInsertOperation] = []
        traffic_updates: list[TrafficUpdateOperation] = []
        active_traffic_keys: set[tuple[Any, int]] = set()
        for row in source_traffic:
            if (
                row["inbound_id"] not in mapping
                and row["inbound_id"] not in planned_source_ids
            ):
                raise PlanValidationError(
                    f"Traffic {row['id']} references unmapped inbound"
                )
            row_columns = set(row.keys())
            target_inbound_id = mapping.get(row["inbound_id"])
            key = (row[traffic_key], target_inbound_id)
            if target_inbound_id is not None:
                active_traffic_keys.add((row[traffic_key], target_inbound_id))
            values = {
                column: row[column]
                for column in traffic_columns
                if column in row_columns and column != "id"
            }
            values["inbound_id"] = target_inbound_id or row["inbound_id"]
            if "email" in row_columns:
                values["email"] = row["email"]
            existing = (
                target_traffic_by_key.get(key)
                if target_inbound_id is not None
                else None
            )
            if existing is None:
                traffic_inserts.append(
                    TrafficInsertOperation(int(row["inbound_id"]), _mapping(values))
                )
            else:
                changed = {
                    column: value
                    for column, value in values.items()
                    if column in set(existing.keys()) and existing[column] != value
                }
                if changed:
                    traffic_updates.append(
                        TrafficUpdateOperation(int(existing["id"]), _mapping(changed))
                    )
        traffic_deletes = tuple(
            int(row["id"])
            for row in target_traffic
            if row["inbound_id"] in managed_target_ids
            and (row[traffic_key], row["inbound_id"]) not in active_traffic_keys
        )

        settings_updates: list[SettingsUpdateOperation] = []
        settings_config = allowlist["tables"]["settings"]
        allowed_settings = settings_config.get("allowed_keys", [])
        source_settings = {
            row["key"]: row
            for row in source.execute("SELECT id, key, value FROM settings;")
        }
        target_settings = {
            row["key"]: row
            for row in target.execute("SELECT id, key, value FROM settings;")
        }
        for key in allowed_settings:
            if _is_host_setting(key):
                continue
            if key in source_settings and (
                key not in target_settings
                or target_settings[key]["value"] != source_settings[key]["value"]
            ):
                settings_updates.append(
                    SettingsUpdateOperation(
                        target_settings[key]["id"] if key in target_settings else None,
                        key,
                        source_settings[key]["value"],
                    )
                )

        xray_json = None
        if (
            "xrayTemplateConfig" in source_settings
            and "xrayTemplateConfig" in target_settings
        ):
            source_xray = _json_object(
                source_settings["xrayTemplateConfig"]["value"],
                "source xrayTemplateConfig",
            )
            target_xray = _json_object(
                target_settings["xrayTemplateConfig"]["value"],
                "target xrayTemplateConfig",
            )
            if not isinstance(
                source_xray.get("routing", {}).get("rules", []), list
            ) or not isinstance(source_xray.get("outbounds", []), list):
                raise PlanValidationError(
                    "xrayTemplateConfig routing.rules and outbounds must be arrays"
                )
            target_xray["routing"] = target_xray.get("routing", {})
            if source_xray.get("routing", {}).get("rules") != target_xray[
                "routing"
            ].get("rules") or source_xray.get("outbounds") != target_xray.get(
                "outbounds"
            ):
                target_xray["routing"]["rules"] = source_xray.get("routing", {}).get(
                    "rules", []
                )
                target_xray["outbounds"] = source_xray.get("outbounds", [])
                xray_json = json.dumps(target_xray, ensure_ascii=False, indent=2)

        return DatabaseSyncPlan(
            target_fingerprint=fingerprint,
            inbound_mapping=tuple(sorted(mapping.items())),
            inbound_creates=tuple(creates),
            inbound_client_updates=tuple(inbound_updates),
            inbound_deletes=tuple(deletes),
            client_inserts=tuple(client_inserts),
            client_updates=tuple(client_updates),
            client_deletes=client_deletes,
            traffic_inserts=tuple(traffic_inserts),
            traffic_updates=tuple(traffic_updates),
            traffic_deletes=traffic_deletes,
            settings_updates=tuple(settings_updates),
            xray_template_json=xray_json,
        )
