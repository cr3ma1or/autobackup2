#!/usr/bin/env bash
# ==============================================================================
# Name:        xui-restore
# Version:     2.5
# Example:     /usr/local/bin/xui-restore --yes xui-backup-20260101T030000Z-1-1.tar.gz.gpg
# Supported:   GNU/Linux, Bash >= 4.4, GNU coreutils, Systemd >= 245
# Description: Restores a verified 3x-ui backup archive: decrypts via the
#              local GPG secret keyring, validates manifest/schema/SQLite
#              integrity, requires explicit confirmation, and preserves a
#              rollback copy of the current database with automatic recovery
#              on failure.
# Author:      cr3ma1or
# Last Change: 2026-09-xx
# License:     MIT License
#
# Compatibility: xui-backup v2.11+ archives (GPG dual-recipient asymmetric
#                encryption; manifest format xui-backup-manifest-v2).
#
# CLI Flags:
#   -y, --yes   Skip interactive confirmations (non-interactive / DR use)
#   -h, --help  Show usage message and exit
#
# Environment Variables:
#   None. This script does not read /etc/x-ui/.env; GPG recipient key
#   selection is automatic via the local secret keyring in $GNUPGHOME.
#
# Requirements:
#   - bash (>= 4.4), python3 (>= 3.6)
#   - coreutils (basename, cat, chmod, date, install, mkdir, mktemp, mv,
#     rm, sha256sum, sleep, sort, stat, timeout)
#   - sqlite3, gpg, gpgconf, tar, findutils (find), util-linux (flock), systemctl
#
# Infrastructure Paths:
#   - Database:        /etc/x-ui/x-ui.db             (root:root 0600)
#   - Backup Store:     /backup/x-ui                  (root:root 0700)
#   - GNUPGHOME:        /etc/x-ui/standby/primary-local-gnupg (root:root 0700)
#   - Lock File:        /run/xui-backup/lock          (shared with xui-backup)
#   - Log File:         /var/log/xui-restore.log      (root:root 0600)
#
# Exit Codes:
#   0   - Successful restore
#   1   - Validation, verification, decryption, restore, or service-start failure
#   2   - CLI usage error
#   3   - User cancellation (YES/RESTORE not confirmed)
#   127 - Required executable was not found
#   130 - Interrupted by SIGINT
#   143 - Terminated by SIGTERM
# ==============================================================================

set -Eeuo pipefail
umask 077

# ------------------------------------------------------------------------------
# 1. Configuration & Constants
# ------------------------------------------------------------------------------
readonly SCRIPT_NAME=${0##*/}
readonly SCRIPT_VERSION="2.5"
readonly LOG_FILE=/var/log/xui-restore.log
readonly COMMAND_TIMEOUT_SECONDS=300
readonly SERVICE_ACTIVE_RETRIES=15
readonly ENV_FILE=/etc/x-ui/.env
readonly BACKUP_DIR=/backup/x-ui
readonly DB_PATH=/etc/x-ui/x-ui.db
readonly DB_DIR=${DB_PATH%/*}
readonly XUI_SERVICE=x-ui
readonly LOCK_DIR=/run/xui-backup
readonly LOCK_FILE="${LOCK_DIR}/lock"
readonly GNUPG_DIR=/etc/x-ui/standby/primary-local-gnupg
export GNUPGHOME="$GNUPG_DIR"
export SCRIPT_VERSION

readonly REQUIRED_COMMANDS=(
  basename cat chmod date find flock gpg gpgconf grep install mkdir mktemp mv
  python3 rm sha256sum sleep sort sqlite3 stat systemctl tar timeout
)

TEMP_DIR=""
LOCK_FD=-1
LOCK_ACQUIRED=0
SERVICE_STOPPED=0
REPLACED_DB=0
ASSUME_YES=0
ROLLBACK_DB=""
ARCHIVE=""
HASH_FILE=""
SELECT_ARG=""
TRUSTED_BACKUP_SIGNER_FINGERPRINT=""

prepare_log_file() {
  if [[ -L "$LOG_FILE" ]]; then
    printf 'Refusing symlink log file: %s\n' "$LOG_FILE" >&2
    exit 1
  fi

  if [[ -e "$LOG_FILE" && ! -f "$LOG_FILE" ]]; then
    printf 'Refusing non-regular log file: %s\n' "$LOG_FILE" >&2
    exit 1
  fi

  if [[ ! -e "$LOG_FILE" ]]; then
    install -m 0600 -o root -g root /dev/null "$LOG_FILE"
  else
    chown -h root:root "$LOG_FILE"
    chmod 0600 "$LOG_FILE"
  fi

  [[ "$(stat -c '%U:%G:%a' "$LOG_FILE")" == 'root:root:600' ]] || {
    printf 'Unsafe log file owner/mode: %s\n' "$LOG_FILE" >&2
    exit 1
  }
}

log() {
  local level=$1
  shift
  local line

  line="$(printf '%s [%s] [%s] [pid=%s] %s' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    "$level" \
    "$SCRIPT_NAME" \
    "$$" \
    "$*")"
  printf '%s\n' "$line" >&2
  [[ -n "${LOG_FILE:-}" ]] && printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null

}

log_info() {
  log INFO "$@"
}

log_warn() {
  log WARN "$@"
}

log_error() {
  log ERROR "$@"
}

# shellcheck disable=SC2317,SC2329,SC2339 # Invoked indirectly via cleanup() in EXIT trap
wipe_file() {
  local target_file="$1"
  [[ -f "$target_file" ]] || return 0

  if command -v shred >/dev/null 2>&1; then
    shred -u -z -n 1 -- "$target_file" 2>/dev/null || rm -f -- "$target_file"
  else
    rm -f -- "$target_file"
  fi
}

# Verifies that a directory and all of its parent components up to "/" are
# owned by root and not group/world-writable — protects against symlink-swap
# or directory-replacement attacks via an insecure ancestor directory.
require_safe_parent_chain() {
  local dir="$1" parent
  parent="$(dirname -- "$dir")"
  while [[ "$parent" != "/" && "$parent" != "." ]]; do
    local mode
    mode="$(stat -c '%U:%a' -- "$parent" 2>/dev/null)" || return 1
    [[ "$mode" == root:* ]] || { log_error "Unsafe ancestor owner: $parent"; return 1; }
    [[ "${mode#*:}" =~ ^[0-7]00$|^[0-7]?[0-5]?[0-5]$ ]] || {
      log_error "Unsafe ancestor permissions: $parent ($mode)"; return 1; }
    parent="$(dirname -- "$parent")"
  done
}

usage() {
  cat <<EOF
Использование: $SCRIPT_NAME [ОПЦИИ] [АРХИВ]

Опции:
  -y, --yes     Не запрашивать подтверждения (автоматический режим)
  -h, --help    Показать справку и выйти

Аргументы:
  АРХИВ         Имя файла в $BACKUP_DIR (по умолчанию: последний валидный)
EOF
}

require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || {
      echo "Не найдена обязательная команда: $cmd" >&2
      exit 127
    }
  done
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        exit 0
        ;;
      -y|--yes)
        ASSUME_YES=1
        shift
        ;;
      --)
        shift
        break
        ;;
      -*)
        echo "Неизвестный параметр: $1" >&2
        usage >&2
        exit 2
        ;;
      *)
        if [[ -n "$SELECT_ARG" ]]; then
          echo 'Можно указать не более одного архива.' >&2
          usage >&2
          exit 2
        fi
        SELECT_ARG="$1"
        shift
        ;;
    esac
  done

  if (($# == 1)) && [[ -n "$SELECT_ARG" ]]; then
    echo 'Можно указать не более одного архива.' >&2
    usage >&2
    exit 2
  fi

  if (($# > 1)); then
    echo 'Можно указать не более одного архива.' >&2
    usage >&2
    exit 2
  fi

  if (($# == 1)); then
    SELECT_ARG="$1"
  fi
}

# Acquires an exclusive, non-blocking lock to prevent concurrent restores.
acquire_lock() {
  install -d -m 0700 -o root -g root "$LOCK_DIR"

  exec {LOCK_FD}>"$LOCK_FILE"
  flock -n "$LOCK_FD" || {
    log_error 'Backup или восстановление x-ui уже выполняется; операция отменена.'
    exit 1
  }
  LOCK_ACQUIRED=1
}

# Restores the previous database and restarts the service after abnormal exit.
# shellcheck disable=SC2317,SC2329,SC2339 # Invoked indirectly via EXIT trap
cleanup() {
  local rc=$?
  trap - EXIT ERR INT TERM

  if [[ "${SERVICE_STOPPED:-0}" == 1 ]]; then
    if [[ "${REPLACED_DB:-0}" == 1 && -n "${ROLLBACK_DB:-}" && -f "$ROLLBACK_DB" ]]; then
      if [[ "$(sqlite3 -readonly "$ROLLBACK_DB" 'PRAGMA integrity_check;' 2>/dev/null)" == ok ]]; then
        log_warn 'Аварийный rollback исходной DB.'

        if mv -f -- "$ROLLBACK_DB" "$DB_PATH"; then
          rm -f -- "${DB_PATH}-wal" "${DB_PATH}-shm" || true
        else
          local emg_bak
          emg_bak="${BACKUP_DIR}/failed-rollback-$(date +%s).db"
          mv -f -- "$ROLLBACK_DB" "$emg_bak" || true
          log_error "Не удалось опубликовать rollback DB; файл сохранён: $emg_bak"
        fi
      else
        local corrupt_bak
        corrupt_bak="${BACKUP_DIR}/corrupted-rollback-$(date +%s).db"
        mv -f -- "$ROLLBACK_DB" "$corrupt_bak" || true
        log_error "Rollback DB повреждена; копия для анализа сохранена: $corrupt_bak"
      fi
    fi

    log_warn "Запуск $XUI_SERVICE после аварийного завершения."
    systemctl start "$XUI_SERVICE" || true
  fi

  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    case "$TEMP_DIR" in
      "$BACKUP_DIR"/.restore_work.*)
        [[ -f "$TEMP_DIR/payload.tar.gz" ]] && wipe_file "$TEMP_DIR/payload.tar.gz"
        [[ -f "$TEMP_DIR/payload/x-ui.db" ]] && wipe_file "$TEMP_DIR/payload/x-ui.db"
        [[ -f "$TEMP_DIR/payload/3xui_export.json" ]] && wipe_file "$TEMP_DIR/payload/3xui_export.json"
        rm -rf -- "$TEMP_DIR" || true
        ;;
      *)
        log_error "Cleanup отказался удалять неожиданный TEMP_DIR: $TEMP_DIR"
        ;;
    esac
  fi

  [[ -n "${ROLLBACK_DB:-}" && -f "$ROLLBACK_DB" && "${SERVICE_STOPPED:-0}" -eq 0 ]] && rm -f -- "$ROLLBACK_DB" || true
  [[ -f "${DB_DIR}/.x-ui.db.restore.new" ]] && rm -f -- "${DB_DIR}/.x-ui.db.restore.new" || true

  if (( LOCK_ACQUIRED == 1 )) && [[ -n "${GNUPGHOME:-}" && -d "$GNUPGHOME" ]]; then
    gpgconf --homedir "$GNUPGHOME" --kill gpg-agent 2>/dev/null || true
  fi


  if (( ${LOCK_FD:--1} >= 0 )); then
    flock -u "$LOCK_FD" || true
    exec {LOCK_FD}>&- || true
    LOCK_FD=-1
  fi

  exit "$rc"
}

# Logs the failing command/line for post-mortem, then exits with its status
# explicitly rather than relying on implicit set -e behavior after ERR fires.
# shellcheck disable=SC2317,SC2329,SC2339 # Invoked indirectly via ERR trap
on_error() {
  local rc=$?
  trap - ERR
  log_error "Ошибка на строке ${BASH_LINENO[0]}: команда \`${BASH_COMMAND}\`, exit_code=$rc"
  # Normalize to a stable contract (see header "Exit Codes"): unexpected
  # runtime failures caught via ERR always surface as exit code 1,
  # regardless of the raw exit status of the failing command.
  exit 1
}

# Marks interruption by signal so it can be distinguished from a normal error.
# shellcheck disable=SC2317,SC2329,SC2339 # Invoked indirectly via INT trap
on_interrupt() {
  trap - INT TERM ERR
  local sig="$1"
  log_warn "Получен сигнал $sig; выполняется корректное завершение."
  case "$sig" in
    INT) exit 130 ;;
    TERM) exit 143 ;;
    *) exit 130 ;;
  esac
}

# Checks privileges, required files, permissions, tools, and environment values.
validate_runtime() {
  [[ $EUID -eq 0 ]] || {
    log_error 'Запустите скрипт с правами root (sudo).'
    exit 1
  }
  require_cmd "${REQUIRED_COMMANDS[@]}"
}

validate() {
  if [[ -e "$ENV_FILE" ]]; then
    [[ ! -L "$ENV_FILE" && -f "$ENV_FILE" ]] || {
      log_error '.env не является обычным файлом или является symlink.'
      exit 1
    }
    [[ "$(stat -c '%U:%G:%a' "$ENV_FILE")" == root:root:600 ]] || {
      log_error 'Небезопасные права .env: ожидается root:root:600.'
      exit 1
    }
  else
    log_error ".env отсутствует: $ENV_FILE"
    exit 1
  fi

  local line value found=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == PRIMARY_LOCAL_RECIPIENT=* ]] || continue
    ((found == 0)) || { log_error 'PRIMARY_LOCAL_RECIPIENT указан более одного раза.'; exit 1; }
    value="${line#PRIMARY_LOCAL_RECIPIENT=}"
    if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    fi
    [[ "$value" =~ ^[0-9A-Fa-f]{40}$ ]] || {
      log_error 'PRIMARY_LOCAL_RECIPIENT должен быть полным 40-символьным GPG fingerprint.'
      exit 1
    }
    TRUSTED_BACKUP_SIGNER_FINGERPRINT="${value^^}"
    found=1
  done < "$ENV_FILE"
  ((found == 1)) || { log_error 'PRIMARY_LOCAL_RECIPIENT отсутствует в .env.'; exit 1; }

  [[ ! -L "$BACKUP_DIR" && -d "$BACKUP_DIR" ]] || {
    echo 'Backup-каталог отсутствует, не является каталогом или является symlink.' >&2
    exit 1
  }

  [[ ! -L "$GNUPGHOME" && -d "$GNUPGHOME" ]] || {
    echo 'GNUPGHOME отсутствует, не является каталогом или является symlink.' >&2
    exit 1
  }

  require_safe_parent_chain "$GNUPGHOME" || exit 1

  [[ -d "$DB_DIR" && ! -L "$DB_DIR" ]] || {
    echo 'Каталог DB отсутствует, не является каталогом или является symlink.' >&2
    exit 1
  }

  if [[ -L "$DB_PATH" ]]; then
    echo 'DB_PATH не должен быть symlink.' >&2
    exit 1
  fi


  if [[ -e "$DB_PATH" ]]; then
    [[ -f "$DB_PATH" ]] || { echo 'DB_PATH не является обычным файлом.' >&2; exit 1; }
    [[ "$(stat -c '%U:%G:%a' "$DB_PATH")" == root:root:600 ]] || { echo 'Небезопасные права текущей DB.' >&2; exit 1; }
  fi

  [[ "$(stat -c '%U:%G:%a' "$BACKUP_DIR")" == root:root:700 ]] || {
    echo 'Небезопасные права backup-каталога: ожидается root:root:700.' >&2
    exit 1
  }

  [[ -d "$DB_DIR" && -w "$DB_DIR" ]] || {
    echo "Каталог DB ($DB_DIR) отсутствует или недоступен для записи." >&2
    exit 1
  }

  [[ "$(stat -c '%U:%G:%a' "$GNUPGHOME")" == "root:root:700" ]] || {
    echo 'Небезопасные права GNUPGHOME: ожидается root:root:700.' >&2
    exit 1
  }

  if ! gpg --batch --list-secret-keys >/dev/null 2>&1; then
    echo 'В GNUPGHOME не обнаружен закрытый ключ для дешифрации архива.' >&2
    exit 1
  fi
}

# Selects the requested archive or the latest matching backup archive.
select_archive() {
  local arg=${1:-}
  local archive hash_file
  local archive_list

  if [[ -n "$arg" ]]; then
    arg="${arg##*/}"
    archive="$BACKUP_DIR/$arg"

    [[ "$archive" == "$BACKUP_DIR/"*.tar.gz.gpg && -f "$archive" ]] || {
      echo "Недопустимый архив: $arg" >&2
      exit 1
    }
  else
    local -a restore_archives=()
    archive_list="$TEMP_DIR/restore_archives.list"

    if ! find "$BACKUP_DIR" -maxdepth 1 -type f -name 'xui-backup-*.tar.gz.gpg' \
        -print0 | LC_ALL=C sort -z >"$archive_list"; then
      echo 'Не удалось получить список backup-архивов.' >&2
      exit 1
    fi

    if ! mapfile -d '' -t restore_archives <"$archive_list"; then
      rm -f -- "$archive_list"
      echo 'Не удалось прочитать список backup-архивов.' >&2
      exit 1
    fi

    rm -f -- "$archive_list" || {
      echo 'Не удалось удалить временный список backup-архивов.' >&2
      exit 1
    }


    ((${#restore_archives[@]} > 0)) || {
      echo 'Бэкапы не найдены.' >&2
      exit 1
    }

    local idx found_valid=0
    for ((idx = ${#restore_archives[@]} - 1; idx >= 0; idx--)); do
      local cand="${restore_archives[idx]}"
      if [[ -f "${cand}.sha256" ]]; then
        archive="$cand"
        found_valid=1
        break
      fi
    done

    ((found_valid == 1)) || {
      echo 'Не найдено ни одного бэкапа с валидным SHA-256 sidecar.' >&2
      exit 1
    }
  fi
    hash_file="${archive}.sha256"
    [[ -f "$hash_file" ]] || {
      echo "Не найден SHA-256 sidecar: $hash_file" >&2
      exit 1
    }

  ARCHIVE="$archive"
  HASH_FILE="$hash_file"
}

verify_archive_checksum() {
  local -a checksum_lines=()
  local checksum_line
  local expected_hash
  local actual_hash

  if ! mapfile -t checksum_lines <"$HASH_FILE"; then
    echo 'SHA-256 sidecar пуст или недоступен для чтения.' >&2
    return 1
  fi

  if ((${#checksum_lines[@]} != 1)); then
    echo 'SHA-256 sidecar должен содержать ровно одну checksum-запись.' >&2
    return 1
  fi

  checksum_line="${checksum_lines[0]}"
  expected_hash="${checksum_line%%[[:space:]]*}"

  if [[ ! "$expected_hash" =~ ^[[:xdigit:]]{64}$ ]]; then
    echo 'SHA-256 sidecar имеет некорректный формат.' >&2
    return 1
  fi

  actual_hash="$(sha256sum -- "$ARCHIVE")"
  actual_hash="${actual_hash%%[[:space:]]*}"

  [[ "$actual_hash" == "$expected_hash" ]]
}

# Validates archive contents, manifest hashes, SQLite integrity, and table list.
verify_payload() {
  local payload=$1
  local out=$2
  local members

  members="$(tar -tzf "$payload" | LC_ALL=C sort)" || {
    echo 'Не удалось получить список файлов из расшифрованного payload.' >&2
    return 1
  }

  case "$members" in
    $'manifest.json\nx-ui.db' | $'3xui_export.json\nmanifest.json\nx-ui.db')
      ;;
    *)
      echo 'Состав архива не соответствует допустимому формату backup.' >&2
      return 1
      ;;
  esac

  tar -xzf "$payload" -C "$out" --no-same-owner --no-same-permissions || {
    echo 'Не удалось распаковать расшифрованный payload.' >&2
    return 1
  }

  if ! python3 - "$out" <<'PY'
import hashlib
import json
import os
import sqlite3
import stat
import sys
from urllib.parse import quote

workdir = sys.argv[1]
manifest_path = os.path.join(workdir, "manifest.json")
db_path = os.path.join(workdir, "x-ui.db")
json_path = os.path.join(workdir, "3xui_export.json")

def digest(path):
    hasher = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()

def require_regular_file(path, label):
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        raise SystemExit(f"Missing required file: {label}")
    if not stat.S_ISREG(mode):
        raise SystemExit(f"Expected a regular file: {label}")

def require_sha256(value, label):
    if not isinstance(value, str) or len(value) != 64:
        raise SystemExit(f"Invalid SHA-256 value in manifest: {label}")
    try:
        int(value, 16)
    except ValueError:
        raise SystemExit(f"Invalid SHA-256 value in manifest: {label}")

actual_names = set(os.listdir(workdir))
allowed_names = {"manifest.json", "x-ui.db", "3xui_export.json"}

if not actual_names.issubset(allowed_names):
    raise SystemExit("Unexpected file found after payload extraction")

require_regular_file(manifest_path, "manifest.json")
require_regular_file(db_path, "x-ui.db")

with open(manifest_path, encoding="utf-8") as source:
    manifest = json.load(source)

if manifest.get("format") != "xui-backup-manifest-v2":
    raise SystemExit("Unknown manifest format")

database_sha256 = manifest.get("database_sha256")
require_sha256(database_sha256, "database_sha256")

if digest(db_path) != database_sha256:
    raise SystemExit("Database hash differs from manifest")

tables = manifest.get("tables")
if (
    not isinstance(tables, list)
    or not tables
    or any(not isinstance(name, str) or not name for name in tables)
    or len(set(tables)) != len(tables)
):
    raise SystemExit("Invalid manifest table list")

json_export = manifest.get("json_export", True)
if not isinstance(json_export, bool):
    raise SystemExit("Manifest json_export must be boolean")

json_exists = os.path.exists(json_path)

if json_export:
    require_regular_file(json_path, "3xui_export.json")
    json_sha256 = manifest.get("json_sha256")
    require_sha256(json_sha256, "json_sha256")
    if digest(json_path) != json_sha256:
        raise SystemExit("JSON hash differs from manifest")
else:
    if json_exists:
        raise SystemExit("json_export=false but JSON export file exists")
    if "json_sha256" in manifest:
        raise SystemExit("json_export=false but json_sha256 exists in manifest")

schema_ddl = manifest.get("schema_ddl")
if schema_ddl is not None:
    if not isinstance(schema_ddl, dict):
        raise SystemExit("Invalid manifest schema_ddl")
    if set(schema_ddl) != set(tables):
        raise SystemExit("Manifest schema_ddl table set differs from tables")
    if any(not isinstance(value, str) for value in schema_ddl.values()):
        raise SystemExit("Manifest schema_ddl contains a non-string DDL value")

row_counts = manifest.get("row_counts")
if row_counts is not None:
    if not isinstance(row_counts, dict):
        raise SystemExit("Invalid manifest row_counts")
    if set(row_counts) != set(tables):
        raise SystemExit("Manifest row_counts table set differs from tables")
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        for value in row_counts.values()
    ):
        raise SystemExit("Manifest row_counts contains an invalid value")

connection = sqlite3.connect(
    "file:" + quote(db_path) + "?mode=ro",
    uri=True,
)
try:
    restored_tables = sorted(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    )
finally:
    connection.close()

if restored_tables != sorted(tables):
    raise SystemExit("Manifest table list differs from DB")
PY
  then
    echo 'Manifest или содержимое расшифрованного payload не прошло проверку.' >&2
    return 1
  fi

  if [[ "$(sqlite3 -readonly "$out/x-ui.db" 'PRAGMA integrity_check;')" != ok ]]; then
    echo 'SQLite integrity_check расшифрованной DB завершился ошибкой.' >&2
    return 1
  fi

  return 0
}

# Prints verified backup metadata before any destructive restore action.
print_restore_preflight() {
  local manifest_path=$1
  local archive_path=$2

  python3 - "$manifest_path" "$archive_path" <<'PY'
import json
import os
import sys

manifest_path, archive_path = sys.argv[1:3]

with open(manifest_path, encoding="utf-8") as source:
    manifest = json.load(source)

def value(name):
    return json.dumps(manifest.get(name), ensure_ascii=False)

tables = manifest.get("tables", [])
row_counts = manifest.get("row_counts", {})

json_export = manifest.get("json_export", True)

if "json_export" in manifest:
    json_export_label = json.dumps(json_export)
else:
    json_export_label = "true (legacy manifest default)"

print()
print("===== RESTORE PREFLIGHT =====")
print(f"Archive: {os.path.basename(archive_path)}")
print(f"Archive size (bytes): {os.path.getsize(archive_path)}")
print(f"Created UTC: {value('created_at_utc')}")
print(f"Hostname: {value('hostname')}")
print(f"Backup script version: {value('script_version')}")
print(f"SQLite version: {value('sqlite_version')}")
print(f"Database file: {value('database_file')}")
print(f"JSON export enabled: {json_export_label}")
print(f"Application tables: {len(tables)}")

if isinstance(row_counts, dict):
    total_rows = sum(
        count
        for count in row_counts.values()
        if isinstance(count, int) and not isinstance(count, bool)
    )
    print(f"Manifest row count total: {total_rows}")

print("Backup table list:")
for table in sorted(tables):
    print(f"  {table}")

print("Backup SQLite integrity: OK")
print("=============================")
PY
}

# Compares application-table names and DDL between current and backup databases.
# Returns 0 for full schema match, 10 for a detected mismatch, and another
# non-zero code for a technical comparison failure.
compare_schema() {
  local current_db=$1
  local backup_db=$2

  python3 - "$current_db" "$backup_db" <<'PY'
import sqlite3
import sys
from urllib.parse import quote

current_path, backup_path = sys.argv[1:3]

def read_schema(path):
    connection = sqlite3.connect(
        "file:" + quote(path) + "?mode=ro",
        uri=True,
    )
    try:
        return {
            name: ddl or ""
            for name, ddl in connection.execute(
                "SELECT name, sql "
                "FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        }
    finally:
        connection.close()

current = read_schema(current_path)
backup = read_schema(backup_path)

only_current = sorted(set(current) - set(backup))
only_backup = sorted(set(backup) - set(current))
ddl_differs = sorted(
    name
    for name in set(current) & set(backup)
    if current[name] != backup[name]
)

if not only_current and not only_backup and not ddl_differs:
    print("Schema comparison: MATCH")
    raise SystemExit(0)

print("Schema comparison: MISMATCH")

if only_current:
    print("Only in current DB:")
    for name in only_current:
        print(f"  {name}")

if only_backup:
    print("Only in backup DB:")
    for name in only_backup:
        print(f"  {name}")

if ddl_differs:
    print("DDL differs:")
    for name in ddl_differs:
        print(f"  {name}")

raise SystemExit(10)
PY
}

# ------------------------------------------------------------------------------
# 3. Core Restore Logic
# ------------------------------------------------------------------------------

# Performs archive verification, database replacement, and rollback-protected restart.
main() {
  parse_args "$@"
  [[ $EUID -eq 0 ]] || {
    echo "Must run as root" >&2
    exit 1
  }
  prepare_log_file
  require_cmd "${REQUIRED_COMMANDS[@]}"

  trap cleanup EXIT
  trap on_error ERR
  trap 'on_interrupt INT' INT
  trap 'on_interrupt TERM' TERM
  validate_runtime
  acquire_lock
  validate

  TEMP_DIR="$(mktemp -d "${BACKUP_DIR}/.restore_work.XXXXXX")"
  chmod 700 "$TEMP_DIR"

  select_archive "$SELECT_ARG"

  log_info "Выбран архив: $ARCHIVE"
  log_info 'Будут выполнены SHA-256, GPG, manifest, JSON, SQLite integrity и rollback-защита.'

  if ! verify_archive_checksum; then
    log_error 'Проверка SHA-256 выбранного архива не пройдена.'
    exit 1
  fi

  log_info 'SHA-256: OK'
  
  local payload="$TEMP_DIR/payload.tar.gz"
  local gpg_status="$TEMP_DIR/gpg.status"
  local out="$TEMP_DIR/payload"
  local restore_new="$DB_DIR/.x-ui.db.restore.new"
  local owner
  local group
  local mode
  local schema_rc=0
  local schema_answer
  local answer

  mkdir -m 700 "$out"

  if ! timeout --foreground "${COMMAND_TIMEOUT_SECONDS}s" \
      gpg --batch --yes --status-fd 3 3>"$gpg_status" \
      --decrypt --output "$payload" "$ARCHIVE"; then
    log_error "GPG-дешифрование не выполнено за ${COMMAND_TIMEOUT_SECONDS} секунд или завершилось ошибкой."
    exit 1
  fi
  if ! grep -q '^\[GNUPG:\] GOODSIG ' "$gpg_status" || \
      ! grep -Fq "[GNUPG:] VALIDSIG ${TRUSTED_BACKUP_SIGNER_FINGERPRINT} " "$gpg_status"; then
    log_error 'Архив не имеет валидной подписи доверенного primary signer.'
    exit 1
  fi

  verify_payload "$payload" "$out" || {
    echo 'Проверка расшифрованного backup не пройдена.' >&2
    exit 1
  }

  print_restore_preflight "$out/manifest.json" "$ARCHIVE" || {
    log_error 'Не удалось вывести проверенные metadata backup.'
    exit 1
  }

  schema_rc=0
  if [[ -f "$DB_PATH" && "$(sqlite3 -readonly "$DB_PATH" 'PRAGMA integrity_check;' 2>/dev/null)" == ok ]]; then
    compare_schema "$DB_PATH" "$out/x-ui.db" || schema_rc=$?
  else
    echo
    echo 'ВНИМАНИЕ: Текущая рабочая DB отсутствует или повреждена! Сравнение схем пропущено.' >&2
    schema_rc=20
  fi

  if ((schema_rc == 10)); then
    echo
    echo 'ВНИМАНИЕ: схема backup отличается от текущей рабочей DB.'
    echo 'Продолжение может потребовать миграции 3x-ui после запуска сервиса.'

    if (( ASSUME_YES == 0 )); then
      if ! read -r -p 'Для подтверждения восстановления при различии схемы введите строго YES: ' schema_answer; then
        echo >&2
        echo 'Отменено: не получено подтверждение YES.' >&2
        exit 3
      fi

      if [[ "$schema_answer" != YES ]]; then
        echo 'Отменено: подтверждение YES не получено.' >&2
        exit 3
      fi
    fi
  elif ((schema_rc == 20)); then
    echo
    echo 'ВНИМАНИЕ: Восстановление выполняется на повреждённую или отсутствующую БД.'
    if (( ASSUME_YES == 0 )); then
      if ! read -r -p 'Для подтверждения восстановления аварийного узла введите строго YES: ' schema_answer; then
        echo >&2
        echo 'Отменено: не получено подтверждение YES.' >&2
        exit 3
      fi
      if [[ "$schema_answer" != YES ]]; then
        echo 'Отменено: подтверждение YES не получено.' >&2
        exit 3
      fi
    fi
  elif ((schema_rc != 0)); then
    echo "Не удалось сравнить schema текущей и backup DB, exit_code=$schema_rc." >&2
    exit "$schema_rc"
  fi

  echo
  echo 'Все проверки backup успешно пройдены.'
  echo "Будет остановлен $XUI_SERVICE, создан rollback текущей DB и выполнена атомарная замена."

  if (( ASSUME_YES == 0 )); then
    if ! read -r -p 'Для продолжения введите строго RESTORE: ' answer; then
      echo >&2
      echo 'Отменено: не получено подтверждение RESTORE.' >&2
      exit 3
    fi

    if [[ "$answer" != RESTORE ]]; then
      echo 'Отменено.' >&2
      exit 3
    fi
  fi

  if [[ -f "$DB_PATH" ]]; then
    owner=$(stat -c %u "$DB_PATH")
    group=$(stat -c %g "$DB_PATH")
    mode=$(stat -c %a "$DB_PATH")
  else
    owner=0
    group=0
    mode=600
  fi

  log_info "Остановка $XUI_SERVICE."
  systemctl stop "$XUI_SERVICE"

  SERVICE_STOPPED=1

  if [[ -f "$DB_PATH" ]]; then
    if [[ "$(sqlite3 -readonly "$DB_PATH" 'PRAGMA integrity_check;' 2>/dev/null)" == ok ]]; then
      ROLLBACK_DB="$DB_DIR/.before-restore.db"
      local rollback_escaped="${ROLLBACK_DB//\'/\'\'}"
      sqlite3 "$DB_PATH" <<SQL
.timeout 8000
.backup '${rollback_escaped}'
SQL
      [[ "$(sqlite3 -readonly "$ROLLBACK_DB" 'PRAGMA integrity_check;')" == ok ]] || {
        echo 'Не удалось создать валидную rollback-копию исходной DB.' >&2
        exit 1
      }
    else
      local corrupt_save
      corrupt_save="${BACKUP_DIR}/corrupted-pre-restore-$(date +%s).db"
      log_warn "Текущая DB повреждена; создаётся защитная копия перед заменой: $corrupt_save"
      cp -f -- "$DB_PATH" "$corrupt_save" || true
      [[ -f "${DB_PATH}-wal" ]] && cp -f -- "${DB_PATH}-wal" "${corrupt_save}-wal" || true
      [[ -f "${DB_PATH}-shm" ]] && cp -f -- "${DB_PATH}-shm" "${corrupt_save}-shm" || true
      ROLLBACK_DB=""
    fi
  fi

  install -o "$owner" -g "$group" -m "$mode" "$out/x-ui.db" "$restore_new"

  if [[ "$(sqlite3 -readonly "$restore_new" 'PRAGMA integrity_check;')" != ok ]]; then
    echo 'Подготовленная DB для публикации не проходит integrity_check.' >&2
    exit 1
  fi

  mv -f -- "$restore_new" "$DB_PATH"
  REPLACED_DB=1

  rm -f -- "${DB_PATH}-wal" "${DB_PATH}-shm"

  systemctl start "$XUI_SERVICE"

  local -i attempt=0
  until systemctl is-active --quiet "$XUI_SERVICE"; do
    ((++attempt))

    if ((attempt >= SERVICE_ACTIVE_RETRIES)); then
      echo 'Сервис не запустился за отведённое время; выполнится rollback.' >&2
      systemctl status --no-pager "$XUI_SERVICE" >&2 || true

      if command -v journalctl >/dev/null 2>&1; then
        journalctl -u "$XUI_SERVICE" -n 50 --no-pager >&2 || true
      fi

      exit 1
    fi

    sleep 1
  done
  SERVICE_STOPPED=0
  REPLACED_DB=0

  log_info 'Восстановление успешно завершено.'
  echo "Application tables: $(sqlite3 -readonly "$DB_PATH" "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%';")"
  exit 0
}

# ------------------------------------------------------------------------------
# 4. Entrypoint
# ------------------------------------------------------------------------------

main "$@"
