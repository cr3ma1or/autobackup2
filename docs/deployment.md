# Deployment & Infrastructure Operations Specification

## 1. Сетевая топология и параметры узлов

┌────────────────────────────────────────────────────────────────────────┐
│ Edge DNS Layer (DNS-Only / Unproxied) │
│ - sub.example.com ──► <SECONDARY_IP> (Secondary Gateway) │
│ - direct.example.com ──► <PRIMARY_IP> (Diagnostic Direct) │
└───────────────────┬────────────────────────────────────────────────────┘
│ HTTPS: <SUBS_PORT> (Subs), <REALITY_PORT> (Reality)
▼
┌────────────────────────────────────────────────────────────────────────┐
│ SECONDARY NODE (Standby Gateway & Storage) │
│ Public IP: <SECONDARY_IP> | OS: Debian 12 | SSH: <SECONDARY_SSH_PORT> │
│ Роли: SSH-приемник, хранилище нулевого доверия, Hot-Standby нода │
│ - Служебный пользователь: xbackup (UID 999, shell: /bin/bash) │
│ - 3x-ui сервисы: Web <STANDBY_WEB_PORT>, Sub <STANDBY_SUB_PORT>, Reality <REALITY_PORT> │
│ - IPTables DNAT (режим STANDBY, задаётся TRANSIT_PORT_MAP): │
│ :<EXTERNAL_PORT> -> <PRIMARY_IP>:<PRIMARY_PORT> │
└───────────────────▲────────────────────────────────────────────────────┘
│ SSH Stream (Forced Command) | Port: <SECONDARY_SSH_PORT>
│ Dual-Recipient GPG AES-256 Encrypted Tarball
┌───────────────────┴────────────────────────────────────────────────────┐
│ PRIMARY NODE (Active Master) │
│ Public IP: <PRIMARY_IP> | OS: Ubuntu 24.04 LTS | SSH: <PRIMARY_SSH_PORT>│
│ Роли: Обработка боевого клиентского трафика, формирование бэкапов │
│ - Пользователь исполнения: root │
│ - 3x-ui сервисы: Web <PRIMARY_WEB_PORT>, Sub <PRIMARY_SUB_PORT>, Reality <REALITY_PORT> │
│ - Исходящий шлюз: wireproxy SOCKS5 (127.0.0.1:40000) │
│ - Конвейер бэкапа: xui-backup (Systemd Timer: 03:20 UTC +/- 20min) │
└────────────────────────────────────────────────────────────────────────┘

## 2. Размещение файлов и права доступа

### Primary Node

- `/usr/local/bin/xui-backup` (`0700 root:root`) — скрипт создания бэкапа.
- `/usr/local/bin/xui-restore` (`0700 root:root`) — скрипт восстановления БД.
- `/etc/x-ui/.env` (`0600 root:root`) — конфигурация шифрования и уведомлений.
- `/etc/x-ui/backup-transfer.env` (`0600 root:root`) — параметры SSH-доставки.
- `/etc/x-ui/id_ed25519_backup` (`0600 root:root`) — приватный SSH-ключ доставки.
- `/backup/x-ui/` (`0700 root:root`) — локальное хранилище архивов.
- `/run/xui-backup/lock` (`0600 root:root`) — runtime-блокировка.

### Secondary Node

- `/opt/xui-backups/` (`0750 root:xbackup`) — корневой каталог хранилища.
- `/opt/xui-backups/.store.lock` (`0600 xbackup:xbackup`) — общий advisory lock публикации и синхронизации.
- `/opt/xui-backups/bin/xui-backup-receiver.sh` (`0755 root:root`) — точка входа SSH Forced Command.
- `/opt/xui-backups/bin/xui-backup-retention.sh` (`0755 root:root`) — скрипт очистки; unit запускает его через `/usr/local/bin/xui-backup-retention`.
- `/etc/x-ui/backup-retention.env` (`0644 root:root`) — несекретная политика retention (`MAX_AGE_DAYS`), читаемая пользователем `xbackup`.
- `/opt/xui-backups/bin/xui-backup-health.sh` (`0755 root:root`) — read-only сенсор SLA.
- `/usr/local/bin/xui-backup-health` (`symlink -> /opt/xui-backups/bin/...`).
- `/opt/xui-backups/incoming/` (`0700 xbackup:xbackup`) — каталог валидных архивов.
- `/opt/xui-backups/invalid/` (`0700 xbackup:xbackup`) — карантин поврежденных файлов.
- `/etc/x-ui/sync.env` (`0600 root:root`) — конфигурация модуля синхронизации.
- `/etc/x-ui/standby-snapshots/` (`0700 root:root`) — корень снимков отката; runtime-пути по умолчанию находятся в `runs/`.
- `/etc/x-ui/standby-mode` (`0644 root:root`) — маркер состояния (`STANDBY` / `PROMOTED`).
- `/run/xui-standby.lock` (`0600 root:root`) — блокировка failover; `/run/xui-standby-sync.lock` (`0600 root:root`) — блокировка процесса синхронизации.

## 3. Шаблоны конфигурационных файлов (`.env`)

### Primary: `/etc/x-ui/.env`

```bash
PRIMARY_LOCAL_RECIPIENT="<GPG_HEX_FINGERPRINT_PRIMARY>"
SECONDARY_SYNC_RECIPIENT="<GPG_HEX_FINGERPRINT_SECONDARY>"
SEND_TELEGRAM=1
TG_BOT_TOKEN="<TG_BOT_TOKEN>"
TG_CHAT_ID="<TG_CHAT_ID>"
TG_PROXY_URL="socks5h://127.0.0.1:40000"
MAX_AGE_DAYS=14
MAX_SIZE_GB=2
KEEP_MIN_ARCHIVES=3
EXPORT_JSON=1
```

### Primary: `/etc/x-ui/backup-transfer.env`

```
TRANSFER_ENABLED=1
TRANSFER_HOST="<SECONDARY_IP>"
TRANSFER_USER="xbackup"
TRANSFER_PORT="<SECONDARY_SSH_PORT>"
TRANSFER_KEY="/etc/x-ui/id_ed25519_backup"
TRANSFER_KNOWN_HOSTS="/etc/x-ui/known_hosts_backup"
TRANSFER_TIMEOUT_SEC=900
```

### Secondary: `/etc/x-ui/sync.env`

```
TARGET_DB_PATH="/etc/x-ui/x-ui.db"
INCOMING_DIR="/opt/xui-backups/incoming"
WORK_DIR="/opt/xui-backups/.work-sync"
SNAPSHOTS_DIR="/etc/x-ui/standby-snapshots/runs"
GNUPG_DIR="/etc/x-ui/standby/secondary-sync-gnupg"
STANDBY_MODE_FILE="/etc/x-ui/standby-mode"
ALLOWLIST_PATH="/etc/x-ui/standby/allowlist.json"

SEND_TELEGRAM=1
TG_BOT_TOKEN="<TG_BOT_TOKEN>"
TG_CHAT_ID="<TG_CHAT_ID>"
TG_PROXY_URL=""
```

## 4. Конфигурация SSH Forced Command

Secondary: `/home/xbackup/.ssh/authorized_keys`

```
from="<PRIMARY_IP>",command="/opt/xui-backups/bin/xui-backup-receiver.sh",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding ssh-ed25519 <PUBLIC_KEY> backup-transport
```

SSHD Hardening (`/etc/ssh/sshd_config.d/xbackup.conf`), устанавливаемый `install.sh` на Secondary (права `0644 root:root`):

```
# Restrict the xbackup user to forced-command-only SSH for backup delivery.
Match User xbackup
    ForceCommand /opt/xui-backups/bin/xui-backup-receiver.sh
    AllowAgentForwarding no
    AllowTcpForwarding no
    PermitTunnel no
    X11Forwarding no
```

После установки конфигурация проверяется командой `sshd -t`, затем служба SSH мягко перезагружается (`reload`, с fallback на `restart`).

## 5. Systemd Units и таймеры

- **Primary Backup Timer (`/etc/systemd/system/xui-backup.timer`):**
  `OnCalendar=*-*-* 03:20:00 UTC`, `RandomizedDelaySec=20min`.

- **Secondary Retention Timer (`/etc/systemd/system/xui-backup-retention.timer`):**
  `OnCalendar=*-*-* 04:45:00 UTC`, `Persistent=true`.

- **Secondary Sync Timer (`/etc/systemd/system/xui-standby-sync.timer`):**
  `OnCalendar=*-*-* 04,16:00:00 UTC`, `RandomizedDelaySec=15min`, `Persistent=true`.

## 6. CLI Standby

- `xui-standby validate --json` — проверка конфигурации и окружения.
- `xui-standby status [--json]` — состояние БД, locks, marker и последнего запуска.
- `xui-standby plan --json` — read-only план слияния.
- `xui-standby sync --dry-run --json` — безопасная проверка без изменений.
- `xui-standby sync --json` — запуск управляемой репликации.
- `xui-standby rollback --run-id <UTC_TIMESTAMP-UUID> --yes` — откат по конкретному снимку; альтернативно `--snapshot <path> --yes`.
