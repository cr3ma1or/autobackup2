# AGENTS.md — 3x-ui Backup & Standby Suite

High-signal operational guide for agents working in this repository.

---

## 1. Context Routing & Ground Truth

Before modifying code or making assumptions, read the specific documentation for your module:

| Domain / Task | Mandatory Specs | Key Context to Extract |
| :--- | :--- | :--- |
| **Bash Scripts** (`xui-backup`, `xui-restore`, receiver, retention, health) | `docs/patterns.md`<br>`docs/deployment.md` | `set -Eeuo pipefail`, trap handling, `umask 077`, non-tmpfs work directory, file permissions. |
| **Python Replication Engine** (`xui_standby_sync`) | `docs/database.md`<br>`docs/business-rules.md`<br>`docs/patterns.md` | Schema DDL, Dual-layer merge, `isolation_level=None`, allowlist filtering, host invariants. |
| **Network & Failover** (`xui-failover`, iptables) | `docs/architecture.md`<br>`docs/business-rules.md` | DNAT/MASQUERADE, STANDBY/PROMOTED state machine, port preservation. |
| **CLI, Logging & Alerts** | `docs/ux-guidelines.md`<br>`docs/monitoring.md` | Log formatting, confirmation tokens, Key-Value logging contract, TG alerts. |

---

## 2. Environment & Verification Commands

### Python Standby Sync Engine (`secondary-node/xui_standby_sync`)
- **Working Directory**: `secondary-node/xui_standby_sync`
- **Install Dev Dependencies**: `pip install -e ".[dev]"`
- **Run Unit Tests**: `uv run --with pytest pytest` (or `pytest tests/ -v` on Linux)
  - *Environment Quirk*: Tests strictly enforce POSIX root ownership (UID 0), `0o700`/`0o600` file permissions, and `fcntl` locks. Full test suite execution requires a GNU/Linux environment.
- **Linting**: `uv run --with ruff ruff check .`
- **Type Checking**: `uv run --with mypy mypy .`

---

## 3. Core Architectural Boundaries & Entrypoints

- **Root**: `install.sh` — Universal installer for Primary / Secondary nodes (`bash >= 4.4`, `systemd`, `root`).
- **Primary Node** (`primary-node/`):
  - `xui-backup.sh`: Encrypted SQLite backup creation & off-site SSH transmission.
  - `xui-restore.sh`: Local backup restoration.
- **Secondary Node** (`secondary-node/`):
  - `xui-backup-receiver.sh`: SSH forced command handling incoming backup archives.
  - `xui-backup-retention.sh`: Backup rotation & retention manager.
  - `xui-backup-health.sh`: Standby node health check probe.
  - `xui_standby_sync/`: Core Python 3.10 replication package (`xui-standby` CLI executable).
    - `planner.py`: Strictly read-only (`mode=ro`) sync plan builder.
    - `engine.py`: Transactional executor applying plan to SQLite target.
    - `database.py`: Schema validation, integrity checks, and Dual-Layer Merge logic.
    - `security.py`: GPG signature validation, checksum verification, file wiping.
    - `rollback.py`: Snapshot management and disaster recovery.

---

## 4. Non-Negotiable Core Invariants

### Python Engine
- **Zero External Runtime Dependencies**: Standard Library ONLY (Python >= 3.10). `pip` dependencies are strictly forbidden in production code.
- **SQLite Transactions**: Always use `sqlite3.connect(..., isolation_level=None)` with explicit `BEGIN IMMEDIATE TRANSACTION;`, `COMMIT;`, and `ROLLBACK;`.
- **Exception Safety**: Never catch blind `except Exception:`. Catch target exceptions explicitly: `except (sqlite3.Error, OSError):`.
- **Secure File Cleanup**: Use `shred -u -z -n 1` before removing sensitive temporary files.

### Bash Scripts
- Strictly `#!/usr/bin/env bash`, `set -Eeuo pipefail`, `umask 077`.
- Enclose script logic inside `main() "$@"`. Double-quote all variables (`"$var"`, `"$(cmd)"`) and path operations (`rm -f -- "$file"`).
- Work files must strictly live in `${BACKUP_DIR}/.work` on physical storage (never `/tmp` or `/run` for backup dumps).

### Database & Host Safety
- **Host Invariants**: Replication MUST NOT overwrite host-specific target settings: `webPort`, `subPort`, `subURI`, TLS paths, `tgBotEnable` (always `false` on Secondary), Inbound 1 (Reality 443).
- **Dual-Layer Merge**: Any insert or update in `clients` MUST simultaneously update the JSON `clients` array inside `inbounds.settings`.
- **Standby Guard**: Operational sync requires `/etc/x-ui/standby-mode` containing `STANDBY`.
- **Snapshot Timing**: Physical database snapshots (`/etc/x-ui/standby-snapshots/`) must be created strictly after `systemctl stop x-ui.service`.

---

## 5. Stop-Rules & Surgical Diffs

1. **Stop-Rule (No Guessing)**: Never guess or assume SQLite table schemas, system utility availability, iptables chain states, or systemd services. If data is missing, stop and issue read-only commands (e.g. `sqlite3 file:/etc/x-ui/x-ui.db?mode=ro "PRAGMA table_info(clients);"`, `ss -tlpn`, `systemctl status x-ui`).
2. **Surgical Diffs Only**: Do not rewrite existing working scripts or modules from scratch. Apply targeted fixes. Preserving existing `flock` locks, error traps, and security checks is mandatory.
