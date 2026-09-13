"""Default paths and policy constants for xui-standby-sync."""

from pathlib import Path

STANDBY_MODE_FILE = Path("/etc/x-ui/standby-mode")
FAILOVER_LOCK_FILE = Path("/run/xui-standby.lock")
SYNC_LOCK_FILE = Path("/run/xui-standby-sync.lock")
STORE_LOCK_FILE = Path("/opt/xui-backups/.store.lock")
INCOMING_DIR = Path("/opt/xui-backups/incoming")
SAFE_WORK_ROOT = Path("/opt/xui-backups")
WORK_DIR = SAFE_WORK_ROOT / ".work-sync"
SNAPSHOTS_DIR = Path("/etc/x-ui/standby-snapshots")
RUN_SNAPSHOTS_DIR = SNAPSHOTS_DIR / "runs"
TARGET_DB_PATH = Path("/etc/x-ui/x-ui.db")
ALLOWLIST_PATH = Path("/etc/x-ui/standby/allowlist.json")
GNUPG_DIR = Path("/etc/x-ui/standby/secondary-sync-gnupg")
LOG_FILE = Path("/var/log/xui-standby-sync.log")

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
DEFAULT_CMD_TIMEOUT = 30
DEFAULT_MAX_AGE_SECONDS = 6 * 3600
DEFAULT_MAX_CLOCK_SKEW_SECONDS = 300
DEFAULT_MAX_UNPACK_SIZE_BYTES = 500 * 1024 * 1024
DEFAULT_MAX_ROLLBACK_COPIES = 5
SERVICE_NAME = "x-ui"
