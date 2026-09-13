"""Planner tests as required by section 11.7 of TZ-UPGRADE.md."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from xui_standby_sync.exceptions import PlanValidationError
from xui_standby_sync.planner import build_database_sync_plan


def _create_full_db(path: Path, *, inbounds: list[dict] | None = None,
                    clients: list[dict] | None = None,
                    traffics: list[dict] | None = None,
                    settings: list[dict] | None = None) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("""CREATE TABLE inbounds (
            id INTEGER PRIMARY KEY, port INTEGER, protocol TEXT,
            tag TEXT, settings TEXT, stream_settings TEXT,
            enable INTEGER DEFAULT 1, sniffing TEXT
        )""")
        conn.execute("""CREATE TABLE clients (
            id INTEGER PRIMARY KEY, inbound_id INTEGER,
            uuid TEXT, email TEXT, enable INTEGER DEFAULT 1,
            total_gb INTEGER DEFAULT 0, expiry_time INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE client_traffics (
            id INTEGER PRIMARY KEY, inbound_id INTEGER,
            email TEXT, up INTEGER DEFAULT 0,
            down INTEGER DEFAULT 0, total INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE settings (
            id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT
        )""")

        for row in (inbounds or []):
            keys = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO inbounds ({keys}) VALUES ({placeholders})", list(row.values()))
        for row in (clients or []):
            keys = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO clients ({keys}) VALUES ({placeholders})", list(row.values()))
        for row in (traffics or []):
            keys = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            conn.execute(f"INSERT INTO client_traffics ({keys}) VALUES ({placeholders})", list(row.values()))
        for row in (settings or []):
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", [row["key"], row["value"]])
        conn.commit()
    os.chmod(path, 0o600)


def _allowlist(*, extra_cols: list[str] | None = None) -> dict[str, Any]:
    client_cols = ["id", "inbound_id", "uuid", "email", "enable", "total_gb", "expiry_time"]
    if extra_cols:
        client_cols += extra_cols
    return {
        "tables": {
            "inbounds": {
                "matching_key": "tag",
                "allowed_columns": [
                    "id", "port", "protocol", "tag", "settings",
                    "stream_settings", "enable", "sniffing"
                ]
            },
            "clients": {
                "matching_key": "uuid",
                "fallback_matching_key": "email",
                "allowed_columns": client_cols
            },
            "client_traffics": {
                "matching_key": "email",
                "allowed_columns": ["id", "inbound_id", "email", "up", "down", "total"]
            },
            "settings": {
                "allowed_keys": ["webPort", "webBasePath"]
            }
        },
        "assert_invariants": ["webPort"]
    }


_PRIMARY_SETTINGS = json.dumps({"clients": []})
_PRIMARY_STREAM = json.dumps({"network": "tcp", "security": "reality"})
_BASE_SETTINGS = json.dumps({"clients": []})
_BASE_STREAM = json.dumps({"network": "tcp"})


class TestPlannerReadOnly:
    """Planner must never write to target DB."""

    def test_planner_never_writes_target_db(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], settings=[{"key": "webPort", "value": "2053"}])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], settings=[{"key": "webPort", "value": "2053"}])

        checksum_before = target.read_bytes()
        build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )
        assert target.read_bytes() == checksum_before, "Planner wrote to target DB!"


class TestPlannerInboundId1:
    """Tests related to the required primary inbound id=1."""

    def test_inbound_id1_required_on_source(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 2, "port": 444, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])

        with pytest.raises(PlanValidationError, match="id=1"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )

    def test_inbound_id1_required_on_target(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])
        _create_full_db(target, inbounds=[
            {"id": 2, "port": 444, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM}
        ])

        with pytest.raises(PlanValidationError, match="id=1"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )

    def test_primary_inbound_only_updates_clients_preserving_stream(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        source_settings = json.dumps({"clients": [{"uuid": "aaa", "email": "a@a.com"}]})
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": source_settings, "stream_settings": _PRIMARY_STREAM}
        ])
        target_stream = json.dumps({"network": "tcp", "security": "reality", "realitySettings": {"key": "val"}})
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": target_stream}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        # Should update clients settings of primary inbound
        assert len(plan.inbound_client_updates) == 1
        assert plan.inbound_client_updates[0].target_inbound_id == 1
        # stream_settings should NOT appear in the update
        update_json = plan.inbound_client_updates[0].settings_json
        clients_parsed = json.loads(update_json)
        assert isinstance(clients_parsed, list)


class TestPlannerSecondaryInbounds:
    """Tests for secondary inbound mapping."""

    def test_new_secondary_creates_inbound_create_operation(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "newsec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert len(plan.inbound_creates) == 1
        assert plan.inbound_creates[0].source_inbound_id == 2

    def test_existing_secondary_updates_client_settings(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        src_settings = json.dumps({"clients": [{"uuid": "bbb", "email": "b@b.com"}]})
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "sec",
             "settings": src_settings, "stream_settings": _BASE_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert any(u.target_inbound_id == 2 for u in plan.inbound_client_updates)

    def test_tag_match_with_different_port_fails(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 9000, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])

        with pytest.raises(PlanValidationError, match="collision"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )

    def test_port_match_with_different_non_empty_tag_fails(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "tag_a",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "tag_b",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])

        with pytest.raises(PlanValidationError, match="collision"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )

    def test_two_matching_target_inbounds_ambiguous_fails(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8443, "protocol": "vless", "tag": "other",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
            {"id": 3, "port": 9000, "protocol": "vless", "tag": "sec",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])

        with pytest.raises(PlanValidationError, match="[Aa]mbiguous"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )

    def test_occupied_reserved_port_prevents_import(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 22, "protocol": "vless", "tag": "ssh_tunnel",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
        ])

        with pytest.raises(PlanValidationError, match="occupied or reserved"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset({22}),
                primary_ip=None, standby_ip=None,
            )

    def test_protected_local_inbound_not_pruned(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 80, "protocol": "http", "tag": "local_http",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset({80}),
            primary_ip=None, standby_ip=None,
        )

        # Port 80 is reserved, so target inbound 2 must NOT be in deletes
        delete_ids = {d.target_inbound_id for d in plan.inbound_deletes}
        assert 2 not in delete_ids

    def test_obsolete_non_protected_inbound_planned_for_prune(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM},
            {"id": 2, "port": 8888, "protocol": "vless", "tag": "stale",
             "settings": _BASE_SETTINGS, "stream_settings": _BASE_STREAM},
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        delete_ids = {d.target_inbound_id for d in plan.inbound_deletes}
        assert 2 in delete_ids


class TestPlannerClients:
    """Tests for client insert, update, delete planning."""

    def test_new_source_client_generates_insert(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], clients=[
            {"id": 1, "inbound_id": 1, "uuid": "uuid-new", "email": "new@test.com"}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert len(plan.client_inserts) == 1

    def test_existing_client_generates_update(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], clients=[
            {"id": 1, "inbound_id": 1, "uuid": "uuid-abc", "email": "a@b.com", "total_gb": 100}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], clients=[
            {"id": 1, "inbound_id": 1, "uuid": "uuid-abc", "email": "a@b.com", "total_gb": 0}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert len(plan.client_updates) == 1
        assert plan.client_updates[0].target_client_id == 1

    def test_empty_source_clients_plans_stale_target_deletion(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], clients=[
            {"id": 1, "inbound_id": 1, "uuid": "old-uuid", "email": "old@test.com"}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert 1 in plan.client_deletes

    def test_client_without_uuid_email_is_validation_error(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], clients=[
            {"id": 1, "inbound_id": 1, "uuid": None, "email": None}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])

        with pytest.raises(PlanValidationError, match="no matching key"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )


class TestPlannerTraffic:
    """Tests for traffic insert, update, delete planning."""

    def test_traffic_inserts_planned(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], traffics=[
            {"id": 1, "inbound_id": 1, "email": "t@test.com", "up": 100, "down": 200}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert len(plan.traffic_inserts) == 1

    def test_orphan_traffic_planned_for_deletion(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], traffics=[
            {"id": 1, "inbound_id": 1, "email": "old@test.com", "up": 0, "down": 0}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert 1 in plan.traffic_deletes


class TestPlannerXrayTemplate:
    """Tests for xrayTemplateConfig handling."""

    def test_malformed_xray_json_produces_validation_error(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], settings=[
            {"key": "webPort", "value": "2053"},
            {"key": "xrayTemplateConfig", "value": "not valid json {{{}"}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], settings=[
            {"key": "webPort", "value": "2053"},
            {"key": "xrayTemplateConfig", "value": "{}"}
        ])

        with pytest.raises(PlanValidationError, match="[Mm]alformed JSON"):
            build_database_sync_plan(
                source_db=source, target_db=target,
                allowlist=_allowlist(), reserved_ports=frozenset(),
                primary_ip=None, standby_ip=None,
            )

    def test_xray_template_changes_only_routing_outbounds(self, tmp_path: Path):
        source = tmp_path / "source.db"
        target = tmp_path / "target.db"
        source_xray = json.dumps({
            "routing": {"rules": [{"type": "field", "outboundTag": "direct"}]},
            "outbounds": [{"tag": "direct", "protocol": "freedom"}],
            "log": {"loglevel": "warning"}
        })
        target_xray = json.dumps({
            "routing": {"rules": []},
            "outbounds": [],
            "log": {"loglevel": "warning"},
            "uniqueTargetKey": "preserve_this"
        })
        _create_full_db(source, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], settings=[
            {"key": "webPort", "value": "2053"},
            {"key": "xrayTemplateConfig", "value": source_xray}
        ])
        _create_full_db(target, inbounds=[
            {"id": 1, "port": 443, "protocol": "vless", "tag": "primary",
             "settings": _PRIMARY_SETTINGS, "stream_settings": _PRIMARY_STREAM}
        ], settings=[
            {"key": "webPort", "value": "2053"},
            {"key": "xrayTemplateConfig", "value": target_xray}
        ])

        plan = build_database_sync_plan(
            source_db=source, target_db=target,
            allowlist=_allowlist(), reserved_ports=frozenset(),
            primary_ip=None, standby_ip=None,
        )

        assert plan.xray_template_json is not None
        merged = json.loads(plan.xray_template_json)
        # Target-specific keys should be preserved
        assert merged.get("uniqueTargetKey") == "preserve_this"
        # Routing and outbounds should come from source
        assert merged["routing"]["rules"] != []
        assert merged["outbounds"] != []
