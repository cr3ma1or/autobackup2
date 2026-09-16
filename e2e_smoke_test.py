#!/usr/bin/env python3
"""Isolated xui_standby_sync E2E smoke test.

The test never touches /etc, /opt, /run, or systemd.  On an unprivileged
WSL account it replaces only the service and filesystem-ownership gates with
local test doubles; database/archive/planner/engine code remains real.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "secondary-node" / "xui_standby_sync"
sys.path.insert(0, str(PACKAGE.parent))

from xui_standby_sync.models import (  # noqa: E402
    NotificationConfig, PathsConfig, RuntimeConfig, RuntimeOptions,
    SecurityConfig, SyncPolicy,
)
from xui_standby_sync.workflow import run_sync  # noqa: E402


class LocalLock:
    def __init__(self, *_args: object, **_kwargs: object) -> None: pass
    def __enter__(self) -> "LocalLock": return self
    def __exit__(self, *_args: object) -> None: pass


_real_lstat = Path.lstat


def local_lstat(path: Path) -> os.stat_result:
    result = _real_lstat(path)
    values = {field: getattr(result, field) for field in dir(result) if field.startswith("st_")}
    values["st_uid"] = 0
    return SimpleNamespace(**values)  # type: ignore[return-value]


def log(message: str) -> None:
    print(f"[E2E] {message}", flush=True)


def sql(path: Path, statement: str, params: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(path) as db:
        db.execute(statement, params)


def make_db(path: Path, *, donor: bool) -> None:
    clients = [
        (1, 1, "11111111-1111-4111-8111-111111111111", "alice"),
        (2, 1, "22222222-2222-4222-8222-222222222222", "bob"),
    ] if donor else []
    reality = {"network": "tcp", "security": "reality", "settings": {
        "publicKey": "STANDBY-PUBLIC-KEY", "privateKey": "STANDBY-PRIVATE-KEY",
        "serverNames": ["standby.example"], "shortIds": ["standby-id"],
    }} if not donor else {"network": "tcp", "security": "reality", "settings": {
        "publicKey": "PRIMARY-PUBLIC-KEY", "privateKey": "PRIMARY-PRIVATE-KEY",
        "serverNames": ["primary.example"], "shortIds": ["primary-id"],
    }}
    settings = {"clients": [{"id": c[2], "email": c[3], "enable": 1} for c in clients]}
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE inbounds (id INTEGER PRIMARY KEY, port INTEGER NOT NULL,
          protocol TEXT NOT NULL, tag TEXT, settings TEXT NOT NULL,
          stream_settings TEXT, enable INTEGER DEFAULT 1, sniffing TEXT);
        CREATE TABLE clients (id INTEGER PRIMARY KEY, inbound_id INTEGER NOT NULL,
          uuid TEXT, email TEXT, enable INTEGER DEFAULT 1, total_gb INTEGER DEFAULT 0,
          expiry_time INTEGER DEFAULT 0, sub_id TEXT, tg_id TEXT);
        CREATE TABLE client_traffics (id INTEGER PRIMARY KEY, inbound_id INTEGER NOT NULL,
          email TEXT NOT NULL, up INTEGER DEFAULT 0, down INTEGER DEFAULT 0, total INTEGER DEFAULT 0);
        CREATE TABLE settings (id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, value TEXT NOT NULL);
        """)
        db.execute("INSERT INTO inbounds VALUES (1,443,'vless','primary',?,?,1,?)",
                   (json.dumps(settings), json.dumps(reality), "{}"))
        db.executemany("INSERT INTO clients VALUES (?,?,?,?,1,0,0,NULL,NULL)", clients)
        db.executemany("INSERT INTO settings(key,value) VALUES (?,?)", [
            ("webPort", "2053" if donor else "60291"), ("subPort", "2096"),
            ("tgBotEnable", "false"), ("subURI", "/standby-sub")])
    os.chmod(path, 0o600)


def make_archive(donor_db: Path, incoming: Path, gpg_home: Path) -> Path:
    payload = incoming.parent / "payload.tar.gz"
    manifest = {"format": "xui-backup-manifest-v2",
                "database_sha256": hashlib.sha256(donor_db.read_bytes()).hexdigest()}
    with tarfile.open(payload, "w:gz") as tar:
        for name, data in (("x-ui.db", donor_db.read_bytes()),
                           ("manifest.json", json.dumps(manifest).encode())):
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o600
            import io
            tar.addfile(info, io.BytesIO(data))
    key_batch = gpg_home / "key.batch"
    key_batch.write_text(
        "Key-Type: RSA\nKey-Length: 2048\nName-Real: E2E Smoke\n"
        "Name-Email: e2e@example.invalid\nExpire-Date: 0\n%no-protection\n%commit\n"
    )
    subprocess.run(["gpg", "--batch", "--homedir", str(gpg_home),
                    "--generate-key", str(key_batch)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    key_batch.unlink()
    fingerprint = next(
        fields[9] for fields in (
            line.split(":") for line in subprocess.check_output(
                ["gpg", "--batch", "--homedir", str(gpg_home),
                 "--list-secret-keys", "--with-colons"], text=True).splitlines()
        ) if len(fields) > 9 and fields[0] == "fpr"
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = incoming / f"xui-backup-{timestamp}-e2e.tar.gz.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--homedir", str(gpg_home),
                    "--trust-model", "always", "--encrypt", "--recipient", fingerprint,
                    "--output", str(archive_name), str(payload)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    archive_name.with_suffix(archive_name.suffix + ".sha256").write_text(
        hashlib.sha256(archive_name.read_bytes()).hexdigest() + "  " + archive_name.name + "\n")
    os.chmod(archive_name, 0o600)
    os.chmod(archive_name.with_suffix(archive_name.suffix + ".sha256"), 0o600)
    for item in gpg_home.rglob("*"):
        if item.name.endswith("~"):
            item.unlink()
        elif item.is_dir():
            os.chmod(item, 0o700)
        else:
            os.chmod(item, 0o600)
    payload.unlink()
    return archive_name


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    sandbox = ROOT / "sandbox"
    if sandbox.exists() and not args.keep: shutil.rmtree(sandbox)
    sandbox.mkdir(mode=0o700, exist_ok=True)
    for name in ("incoming", "work", "target", "snapshots", "gnupg"):
        (sandbox / name).mkdir(mode=0o700, exist_ok=True)
    (sandbox / "standby-mode").write_text("STANDBY\n"); os.chmod(sandbox / "standby-mode", 0o600)
    allowlist = {
      "tables": {
        "inbounds": {"matching_key":"tag", "allowed_columns":["id","port","protocol","tag","settings","stream_settings","enable","sniffing"]},
        "clients": {"matching_key":"uuid", "fallback_matching_key":"email", "allowed_columns":["id","inbound_id","uuid","email","enable","total_gb","expiry_time","sub_id","tg_id"]},
        "client_traffics": {"matching_key":"email", "allowed_columns":["id","inbound_id","email","up","down","total"]},
        "settings": {"allowed_keys":["webPort","subPort","subURI","tgBotEnable"]}},
      "assert_invariants":["webPort","subPort","tgBotEnable","subURI"]}
    (sandbox / "allowlist.json").write_text(json.dumps(allowlist)); os.chmod(sandbox / "allowlist.json", 0o600)
    env_file = sandbox / ".env"
    env_file.write_text("\n".join(f"{key}={sandbox / value}" for key, value in {
        "INCOMING_DIR":"incoming", "WORK_DIR":"work", "TARGET_DB_PATH":"target/x-ui.db",
        "SNAPSHOTS_DIR":"snapshots", "GNUPG_DIR":"gnupg", "STANDBY_MODE_FILE":"standby-mode",
    }.items()) + "\n")
    os.chmod(env_file, 0o600)
    donor = sandbox / "donor.db"; target = sandbox / "target" / "x-ui.db"
    make_db(donor, donor=True); make_db(target, donor=False)
    archive = make_archive(donor, sandbox / "incoming", sandbox / "gnupg")
    log(f"sandbox={sandbox} (изоляция: OK), archive={archive.name}")
    paths = PathsConfig(target, sandbox/"allowlist.json", sandbox/"incoming", sandbox/"work", sandbox/"snapshots", sandbox/"gnupg", sandbox/"sync.lock", sandbox/"store.lock", sandbox/"standby-mode", sandbox/"failover.lock", sandbox/"sync.log")
    config = RuntimeConfig(paths, SecurityConfig(frozenset(), False, 500*1024*1024, True), SyncPolicy("x-ui",30,6*3600,300,5,frozenset(),"PRIMARY","STANDBY"), NotificationConfig(False,None,None,None), RuntimeOptions(False,True,True,True,True))
    with patch("xui_standby_sync.workflow.LockSet", LocalLock), patch("xui_standby_sync.workflow.stop_service"), patch("xui_standby_sync.workflow.start_service"), patch("xui_standby_sync.workflow.verify_file_security"), patch("xui_standby_sync.database.verify_file_security"), patch("xui_standby_sync.archive.verify_directory_security"), patch("xui_standby_sync.archive.verify_directory_chain"), patch.object(Path, "lstat", local_lstat):
        planned = run_sync(config, backup_path=archive)
    log(f"plan/dry-run exit={planned.exit_code}; clients_insert={len(planned.plan.database.client_inserts) if planned.plan else 'n/a'}")
    if not planned.success or not planned.plan or len(planned.plan.database.client_inserts) != 2: return 1
    config = RuntimeConfig(paths, config.security, config.policy, config.notifications, RuntimeOptions(False,False,True,True,True))
    with patch("xui_standby_sync.workflow.LockSet", LocalLock), patch("xui_standby_sync.workflow.stop_service"), patch("xui_standby_sync.workflow.start_service"), patch("xui_standby_sync.workflow.verify_file_security"), patch("xui_standby_sync.database.verify_file_security"), patch("xui_standby_sync.archive.verify_directory_security"), patch("xui_standby_sync.archive.verify_directory_chain"), patch.object(Path, "lstat", local_lstat):
        result = run_sync(config, backup_path=archive)
    log(f"sync exit={result.exit_code}; success={result.success}; rollback={result.rollback_attempted}")
    with sqlite3.connect(target) as db:
        rows = db.execute("SELECT uuid,email FROM clients ORDER BY id").fetchall()
        obj = json.loads(db.execute("SELECT settings FROM inbounds WHERE id=1").fetchone()[0])
        settings = dict(db.execute("SELECT key,value FROM settings").fetchall())
        reality = json.loads(db.execute("SELECT stream_settings FROM inbounds WHERE id=1").fetchone()[0])
    checks = {"clients table": len(rows) == 2, "dual-layer": {x["uuid"] for x in obj["clients"]} == {x[0] for x in rows}, "webPort": settings["webPort"] == "60291", "subPort": settings["subPort"] == "2096", "tgBotEnable": settings["tgBotEnable"] == "false", "Reality identity": reality["settings"]["publicKey"] == "STANDBY-PUBLIC-KEY" and reality["settings"]["privateKey"] == "STANDBY-PRIVATE-KEY"}
    for name, ok in checks.items(): log(f"{name}: {'PASS' if ok else 'FAIL'}")
    return 0 if result.success and all(checks.values()) else 1

if __name__ == "__main__": raise SystemExit(main())
