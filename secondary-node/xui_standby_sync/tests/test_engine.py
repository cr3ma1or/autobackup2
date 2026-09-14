"""Engine tests as required by section 11.8 of TZ-UPGRADE.md."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from xui_standby_sync.engine import apply_database_sync_plan
from xui_standby_sync.exceptions import (
    OperationCancelledError,
    PlanExecutionError,
    PlanValidationError,
)
from xui_standby_sync.models import (
    ClientInsertOperation,
    ClientUpdateOperation,
    DatabaseFingerprint,
    DatabaseSyncPlan,
    InboundClientSettingsUpdateOperation,
    InboundCreateOperation,
    SettingsUpdateOperation,
    TrafficInsertOperation,
    TrafficUpdateOperation,
)


def _create_target_db(path: Path) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("""CREATE TABLE inbounds (
            id INTEGER PRIMARY KEY, port INTEGER, protocol TEXT,
            tag TEXT, settings TEXT, stream_settings TEXT,
            enable INTEGER DEFAULT 1
        )""")
        conn.execute("""CREATE TABLE clients (
            id INTEGER PRIMARY KEY, inbound_id INTEGER,
            uuid TEXT, email TEXT, enable INTEGER DEFAULT 1,
            total_gb INTEGER DEFAULT 0, expiry_time INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE client_traffics (
            id INTEGER PRIMARY KEY, inbound_id INTEGER,
            email TEXT, up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, total INTEGER DEFAULT 0
        )""")
        conn.execute("""CREATE TABLE settings (
            id INTEGER PRIMARY KEY, key TEXT UNIQUE, value TEXT
        )""")
        conn.execute("""INSERT INTO inbounds (id, port, protocol, tag, settings, stream_settings)
            VALUES (1, 443, 'vless', 'primary', '{"clients":[]}', '{"network":"tcp"}')""")
        conn.execute("INSERT INTO settings (key, value) VALUES ('webPort', '2053')")
        conn.commit()
    os.chmod(path, 0o600)


def _get_fingerprint(path: Path) -> DatabaseFingerprint:
    conn = sqlite3.connect(str(path))
    sv = int(conn.execute("PRAGMA schema_version;").fetchone()[0])
    dv = int(conn.execute("PRAGMA data_version;").fetchone()[0])
    conn.close()
    return DatabaseFingerprint(schema_version=sv, data_version=dv)


def _empty_plan(path: Path, **overrides: Any) -> DatabaseSyncPlan:
    fp = _get_fingerprint(path)
    defaults: dict[str, Any] = dict(
        target_fingerprint=fp,
        inbound_mapping=(),
        inbound_creates=(),
        inbound_client_updates=(),
        inbound_deletes=(),
        client_inserts=(),
        client_updates=(),
        client_deletes=(),
        traffic_inserts=(),
        traffic_updates=(),
        traffic_deletes=(),
        settings_updates=(),
        xray_template_json=None,
        validation_errors=(),
    )
    defaults.update(overrides)
    return DatabaseSyncPlan(**defaults)


class TestEngineValidPlan:
    """Valid plan applies all operation classes correctly."""

    def test_valid_plan_applies_all_operations(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)
        fp = _get_fingerprint(path)

        plan = DatabaseSyncPlan(
            target_fingerprint=fp,
            inbound_mapping=((1, 1),),
            inbound_creates=(),
            inbound_client_updates=(
                InboundClientSettingsUpdateOperation(1, '{"clients":[{"uuid":"u1"}]}'),
            ),
            inbound_deletes=(),
            client_inserts=(
                ClientInsertOperation(1, MappingProxyType(
                    {"inbound_id": 1, "uuid": "u1", "email": "u1@test.com"}
                )),
            ),
            client_updates=(),
            client_deletes=(),
            traffic_inserts=(),
            traffic_updates=(),
            traffic_deletes=(),
            settings_updates=(
                SettingsUpdateOperation(1, "webPort", "8080"),
            ),
            xray_template_json=None,
        )

        apply_database_sync_plan(target_db=path, plan=plan)

        conn = sqlite3.connect(str(path))
        row = conn.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()
        assert row[0] == "8080"
        client = conn.execute("SELECT uuid FROM clients WHERE uuid='u1'").fetchone()
        assert client is not None
        conn.close()

    def test_new_inbound_inserted_before_client_traffic(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)
        fp = _get_fingerprint(path)

        plan = DatabaseSyncPlan(
            target_fingerprint=fp,
            inbound_mapping=((1, 1), (99, 99)),
            inbound_creates=(
                InboundCreateOperation(99, MappingProxyType(
                    {"port": 9000, "protocol": "vless", "tag": "new",
                     "settings": '{"clients":[]}', "stream_settings": "{}"}
                )),
            ),
            inbound_client_updates=(),
            inbound_deletes=(),
            client_inserts=(
                ClientInsertOperation(99, MappingProxyType(
                    {"inbound_id": 99, "uuid": "u-new", "email": "new@test.com"}
                )),
            ),
            client_updates=(),
            client_deletes=(),
            traffic_inserts=(
                TrafficInsertOperation(99, MappingProxyType(
                    {"inbound_id": 99, "email": "new@test.com", "up": 0, "down": 0}
                )),
            ),
            traffic_updates=(),
            traffic_deletes=(),
            settings_updates=(),
            xray_template_json=None,
        )

        apply_database_sync_plan(target_db=path, plan=plan)

        conn = sqlite3.connect(str(path))
        new_inbound = conn.execute("SELECT id FROM inbounds WHERE tag='new'").fetchone()
        assert new_inbound is not None
        real_id = new_inbound[0]
        client = conn.execute(
            "SELECT inbound_id FROM clients WHERE uuid='u-new'"
        ).fetchone()
        assert client[0] == real_id
        conn.close()

    def test_source_to_target_inbound_mapping_resolved_correctly(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)
        fp = _get_fingerprint(path)

        plan = DatabaseSyncPlan(
            target_fingerprint=fp,
            inbound_mapping=((1, 1), (42, 42)),
            inbound_creates=(
                InboundCreateOperation(42, MappingProxyType(
                    {"port": 7777, "protocol": "trojan", "tag": "mapped",
                     "settings": "{}", "stream_settings": "{}"}
                )),
            ),
            inbound_client_updates=(),
            inbound_deletes=(),
            client_inserts=(
                ClientInsertOperation(42, MappingProxyType(
                    {"inbound_id": 42, "uuid": "mapped-uuid", "email": "m@test.com"}
                )),
            ),
            client_updates=(),
            client_deletes=(),
            traffic_inserts=(),
            traffic_updates=(),
            traffic_deletes=(),
            settings_updates=(),
            xray_template_json=None,
        )

        apply_database_sync_plan(target_db=path, plan=plan)

        conn = sqlite3.connect(str(path))
        new_ib = conn.execute("SELECT id FROM inbounds WHERE tag='mapped'").fetchone()
        assert new_ib is not None
        cl = conn.execute("SELECT inbound_id FROM clients WHERE uuid='mapped-uuid'").fetchone()
        assert cl[0] == new_ib[0]
        conn.close()


class TestEngineRejection:
    """Engine rejects invalid plans and fingerprint mismatches."""

    def test_invalid_plan_rejected_before_transaction(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)
        fp = _get_fingerprint(path)

        plan = _empty_plan(path, validation_errors=("some error",))

        with pytest.raises(PlanValidationError, match="invalid database plan"):
            apply_database_sync_plan(target_db=path, plan=plan)

    def test_fingerprint_mismatch_rejects_before_mutation(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)

        wrong_fp = DatabaseFingerprint(schema_version=999, data_version=999)
        plan = _empty_plan(path, target_fingerprint=wrong_fp)

        with pytest.raises(PlanExecutionError, match="fingerprint"):
            apply_database_sync_plan(target_db=path, plan=plan)

        # DB must be unchanged
        conn = sqlite3.connect(str(path))
        row = conn.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()
        assert row[0] == "2053"
        conn.close()


class TestEngineRollback:
    """Failures during apply trigger full rollback."""

    def test_failure_after_inbound_insert_rolls_back_all(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)
        fp = _get_fingerprint(path)

        plan = DatabaseSyncPlan(
            target_fingerprint=fp,
            inbound_mapping=((1, 1), (50, 50)),
            inbound_creates=(
                InboundCreateOperation(50, MappingProxyType(
                    {"port": 5050, "protocol": "vless", "tag": "fail_after",
                     "settings": "{}", "stream_settings": "{}"}
                )),
            ),
            inbound_client_updates=(),
            inbound_deletes=(),
            client_inserts=(
                ClientInsertOperation(50, MappingProxyType(
                    {"inbound_id": 50, "uuid": "will-fail", "email": "fail@test.com"}
                )),
            ),
            client_updates=(
                # This will fail with a bad column name
                ClientUpdateOperation(99999, MappingProxyType({"uuid": "forced-fail"})),
            ),
            client_deletes=(),
            traffic_inserts=(),
            traffic_updates=(),
            traffic_deletes=(),
            settings_updates=(),
            xray_template_json=None,
        )

        # We don't expect rollback to raise here — engine handles it internally
        # and re-raises the original error
        try:
            apply_database_sync_plan(target_db=path, plan=plan)
        except Exception:
            pass

        conn = sqlite3.connect(str(path))
        new_ib = conn.execute("SELECT id FROM inbounds WHERE tag='fail_after'").fetchone()
        # After rollback, the new inbound should not exist
        assert new_ib is None, "Inbound was not rolled back after failure"
        conn.close()

    def test_failure_after_client_delete_rolls_back_all(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)

        with sqlite3.connect(str(path)) as conn:
            conn.execute(
                "INSERT INTO clients (id, inbound_id, uuid, email) VALUES (1,1,'keep-me','keep@test.com')"
            )
            conn.commit()

        fp = _get_fingerprint(path)

        plan = DatabaseSyncPlan(
            target_fingerprint=fp,
            inbound_mapping=((1, 1),),
            inbound_creates=(),
            inbound_client_updates=(),
            inbound_deletes=(),
            client_inserts=(),
            client_updates=(),
            client_deletes=(1,),   # delete existing client
            traffic_inserts=(),
            traffic_updates=(
                # Force failure: non-existent traffic id
                TrafficUpdateOperation(99999, MappingProxyType({"email": "x"})),
            ),
            traffic_deletes=(),
            settings_updates=(),
            xray_template_json=None,
        )

        try:
            apply_database_sync_plan(target_db=path, plan=plan)
        except Exception:
            pass

        conn = sqlite3.connect(str(path))
        row = conn.execute("SELECT id FROM clients WHERE uuid='keep-me'").fetchone()
        assert row is not None, "Client was not restored after rollback"
        conn.close()

    def test_cancellation_in_middle_rolls_back(self, tmp_path: Path):
        path = tmp_path / "target.db"
        _create_target_db(path)
        fp = _get_fingerprint(path)

        call_count = 0

        def cancel_after_two() -> bool:
            nonlocal call_count
            call_count += 1
            return call_count > 2

        plan = DatabaseSyncPlan(
            target_fingerprint=fp,
            inbound_mapping=((1, 1),),
            inbound_creates=(),
            inbound_client_updates=(
                InboundClientSettingsUpdateOperation(1, '{"clients":[]}'),
            ),
            inbound_deletes=(),
            client_inserts=(
                ClientInsertOperation(1, MappingProxyType(
                    {"inbound_id": 1, "uuid": "cancel-test", "email": "c@test.com"}
                )),
            ),
            client_updates=(),
            client_deletes=(),
            traffic_inserts=(),
            traffic_updates=(),
            traffic_deletes=(),
            settings_updates=(),
            xray_template_json=None,
        )

        with pytest.raises(OperationCancelledError):
            apply_database_sync_plan(target_db=path, plan=plan, is_cancelled=cancel_after_two)

        conn = sqlite3.connect(str(path))
        row = conn.execute("SELECT id FROM clients WHERE uuid='cancel-test'").fetchone()
        assert row is None, "Cancelled operation was not rolled back"
        conn.close()
