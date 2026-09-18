# AGENTS.md — 3x-ui Replication & Standby Suite

High-signal operational guide. Follow strictly.

---

## 1. Context Routing & Ground Truth (Lazy-Load Only)

> **SELECTIVE READING ONLY:** You are strictly FORBIDDEN from reading all docs at once. Read ONLY the single file required for the active task.

- **Data Plane & SQLite**: Read `docs/database.md` (or `docs/business-rules.md` for host invariants).
- **Bash, Ops & Hardening**: Read `docs/patterns.md` (code standards) or `docs/deployment.md` (paths, systemd, permissions).
- **Failover & Networking**: Read `docs/architecture.md`.
- **CLI, Logging & Health**: Read `docs/ux-guidelines.md`.

---

## 2. Verification Commands (Linux / WSL2)

- Working directory: `secondary-node/xui_standby_sync`
- Lint & Types: `uv run --with ruff ruff check .` && `uv run --with mypy mypy .`
- Tests: `uv run --with pytest pytest` (Requires Linux UID 0 & POSIX permissions for full pass).

---

## 3. Architecture Scope

- **Root**: `install.sh` (Universal host installer).
- **Primary Node** (`primary-node/`): Encrypted backup creation (`xui-backup.sh`), local restore (`xui-restore.sh`).
- **Secondary Node** (`secondary-node/`): SSH receiver (`xui-backup-receiver.sh`), retention (`xui-backup-retention.sh`), SLA health probe (`xui-backup-health.sh`), and the Python replication engine (`xui_standby_sync/` -> CLI `xui-standby`).

---

## 4. Non-Negotiable Invariants

- **Python Runtime**: Standard Library ONLY (>= 3.10). No external pip dependencies.
- **SQLite Engine**: `sqlite3.connect(..., isolation_level=None)` with explicit `BEGIN IMMEDIATE;`, `COMMIT;`, `ROLLBACK;`.
- **Dual-Layer Merge**: Every client mutation in the `clients` table MUST update the JSON `clients` array inside `inbounds.settings` synchronously in the same transaction.
- **Standby Isolation**: Never overwrite target `webPort`, `subPort`, `subURI`, local TLS paths, `tgBotEnable=false`, or Inbound 1 Reality network identity (ports/keys/SNI/externalProxy).
- **Snapshots & WAL**: Target DB snapshots (`/etc/x-ui/standby-snapshots/`) are taken strictly AFTER stopping `x-ui.service`. Purge `-wal` and `-shm` before restore.
- **Bash Hardening**: `#!/usr/bin/env bash`, `set -Eeuo pipefail`, `umask 077`. Double-quote all expansions. Return 130 on SIGINT, 143 on SIGTERM. Wipe secrets via `shred -u -z -n 1`.

---

## 5. Stop-Rules & Surgical Diffs

1. **Stop-Rule**: Never guess schemas, open ports, or systemd states. Stop and issue read-only commands (`sqlite3 ... PRAGMA`, `ss -tlpn`, `systemctl status`).
2. **Surgical Diffs Only**: Do not rewrite modules or scripts from scratch. Apply minimal targeted unified diffs preserving existing error traps, locks, and security boundaries.
