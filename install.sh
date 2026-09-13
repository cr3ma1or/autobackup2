#!/usr/bin/env bash
# ==============================================================================
# Name:        install.sh
# Version:     1.0
# Description: Universal installer for the 3x-ui autobackup stack.
#              Works BOTH on primary and secondary nodes. Role is auto-detected
#              or forced via --role.
#
# Roles:
#   primary   - installs xui-backup + xui-restore, systemd timer, logrotate,
#               local GPG keyring and off-site SSH transfer scaffolding.
#   secondary - installs xui-backup-receiver (SSH forced command), retention,
#               health probe, python sync engine (venv), systemd units,
#               logrotate, allowlist and sync.env scaffolding.
#
# Usage:
#   ./install.sh                      # auto-detect role
#   ./install.sh --role secondary     # force a role
#   ./install.sh --unattended         # do not prompt, only warn on missing keys
#   ./install.sh --help
#
# Exit codes:
#   0 - success
#   1 - runtime failure
#   2 - invalid CLI usage
#
# Requirements: root, bash >= 4.4, GNU coreutils, python3 >= 3.10 (secondary),
#               apt-get/dnf/yum, systemd.
# ==============================================================================

set -Eeuo pipefail
umask 077

# Error and signal handling
cleanup_exit() {
  # Cleanup resources on normal exit
  :
}

cleanup_error() {
  trap - ERR EXIT
  local exit_code="$1"
  local line_num="$2"
  local command="$3"
  error "Script failed at line $line_num, command: $command"
  exit "$exit_code"
}

cleanup_signal() {
  local signal="$1"
  warn "Received signal $signal, terminating installation"
  exit "$([[ "$signal" == "INT" ]] && echo 130 || echo 143)"
}

trap cleanup_exit EXIT
trap 'cleanup_error "$?" "${BASH_LINENO[0]}" "$BASH_COMMAND"' ERR
trap 'cleanup_signal INT' INT
trap 'cleanup_signal TERM' TERM

# ------------------------------------------------------------------------------
# Constants (keep in sync with the scripts' documented paths)
# ------------------------------------------------------------------------------
readonly SCRIPT_NAME="${0##*/}"
readonly ROLE_AUTO="auto"
readonly ROLE_PRIMARY="primary"
readonly ROLE_SECONDARY="secondary"

readonly PRIMARY_BIN_DIR="/usr/local/bin"
readonly PRIMARY_ENV_FILE="/etc/x-ui/.env"
readonly PRIMARY_TRANSFER_ENV_FILE="/etc/x-ui/backup-transfer.env"
readonly PRIMARY_BACKUP_DIR="/backup/x-ui"
readonly PRIMARY_GNUPG_DIR="/etc/x-ui/standby/primary-local-gnupg"
readonly PRIMARY_SSH_KEY="/etc/x-ui/id_ed25519_backup"
readonly PRIMARY_KNOWN_HOSTS="/etc/x-ui/known_hosts_backup"

readonly SB_BASE_DIR="/opt/xui-backups"
readonly SB_BIN_DIR="${SB_BASE_DIR}/bin"
readonly SB_INCOMING_DIR="${SB_BASE_DIR}/incoming"
readonly SB_INVALID_DIR="${SB_BASE_DIR}/invalid"
readonly SB_LOG_FILE="${SB_BASE_DIR}/receiver.log"
readonly SB_LOCK_FILE="${SB_BASE_DIR}/.store.lock"
readonly SB_USER="xbackup"
readonly SB_SYNC_DIR="/opt/xui-standby"
readonly SB_VENV_DIR="${SB_SYNC_DIR}/venv"
readonly SB_SYNC_ENV_FILE="/etc/x-ui/sync.env"
readonly SB_ALLOWLIST_FILE="/etc/x-ui/standby/allowlist.json"
readonly SB_GNUPG_DIR="/etc/x-ui/standby/secondary-sync-gnupg"
readonly SB_STANDBY_MODE_FILE="/etc/x-ui/standby-mode"
readonly SB_SNAPSHOTS_DIR="/etc/x-ui/standby-snapshots"
readonly SB_WORK_SYNC_DIR="${SB_BASE_DIR}/.work-sync"
readonly SB_SSH_DIR="/home/${SB_USER}/.ssh"
readonly SB_AUTH_KEYS="${SB_SSH_DIR}/authorized_keys"
readonly SB_SYNC_LOG_FILE="/var/log/xui-standby-sync.log"

readonly SYSTEMD_DIR="/etc/systemd/system"
readonly LOGROTATE_DIR="/etc/logrotate.d"

ROLE="${ROLE_AUTO}"
UNATTENDED=0
NO_SYSTEMD=0
REPO_ROOT=""

# ------------------------------------------------------------------------------
# Logging & failure helpers
# ------------------------------------------------------------------------------
log() {
  local level="$1"
  shift
  local timestamp
  timestamp="$(date -u +'%Y-%m-%d %H:%M:%S UTC')"
  printf '%s [install] [PID:%s] [%s] %s\n' "$timestamp" "$$" "$level" "$*"
}

info()  { log INFO "$*"; }
warn()  { log WARN "$*" >&2; }
error() { log ERROR "$*" >&2; }
die()   { log ERROR "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [options]

Options:
  --role <primary|secondary|auto>   Force node role (default: auto-detect)
  --unattended                      Non-interactive; never prompt
  --no-systemd                      Do not install/enable systemd units & timers
  -h, --help                        Show this help and exit
EOF
}

# ------------------------------------------------------------------------------
# Argument parsing
# ------------------------------------------------------------------------------
parse_args() {
  while (( $# > 0 )); do
    case "$1" in
      --role)
        (( $# >= 2 )) || { usage >&2; exit 2; }
        ROLE="$2"
        shift 2
        ;;
      --role=*)
        ROLE="${1#*=}"
        shift
        ;;
      --unattended)
        UNATTENDED=1
        shift
        ;;
      --no-systemd)
        NO_SYSTEMD=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        usage >&2
        exit 2
        ;;
    esac
  done

  case "$ROLE" in
    "$ROLE_AUTO"|"$ROLE_PRIMARY"|"$ROLE_SECONDARY") : ;;
    *) echo "Invalid --role value: $ROLE (expected primary|secondary|auto)" >&2; exit 2 ;;
  esac
}

# ------------------------------------------------------------------------------
# Environment, root & dependency checks
# ------------------------------------------------------------------------------
require_root() {
  [[ "$EUID" -eq 0 ]] || die "Must run as root (sudo)"
}

require_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || die "Required command not found: $cmd"
  done
}

check_bash() {
  if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
    die "bash >= 4.4 is required (found $BASH_VERSION)"
  fi
}

detect_pkg_manager() {
  local mgr
  for mgr in apt-get dnf yum; do
    command -v "$mgr" >/dev/null 2>&1 && { PKG_MANAGER="$mgr"; return 0; }
  done
  warn "No supported package manager found (apt-get/dnf/yum); skipping package install"
  PKG_MANAGER=""
}

install_packages() {
  local -a pkgs=("$@")
  (( ${#pkgs[@]} > 0 )) || return 0
  [[ -n "$PKG_MANAGER" ]] || return 0
  info "Installing packages: ${pkgs[*]}"
  case "$PKG_MANAGER" in
    apt-get)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -y
      apt-get install -y -- "${pkgs[@]}"
      ;;
    dnf|yum)
      "$PKG_MANAGER" install -y -- "${pkgs[@]}"
      ;;
  esac
}

# ------------------------------------------------------------------------------
# Shared helpers
# ------------------------------------------------------------------------------
install_file() {
  local src="$1" dst="$2" mode="${3:-0644}"
  local owner="${4:-root:root}"
  local dir="$(dirname -- "$dst")"
  # Ensure parent directory exists without changing its permissions
  if [[ ! -d "$dir" ]]; then
    install -d -m 0755 -o root -g root "$dir"
  fi
  install -o "${owner%%:*}" -g "${owner##*:}" -m "$mode" -- "$src" "$dst"
}

write_unit() {
  local name="$1" body="$2"
  local dst="${SYSTEMD_DIR}/${name}"
  printf '%s' "$body" >"$dst"
  chmod 0644 "$dst"
  info "Wrote systemd unit: $dst"
}

ensure_dir() {
  local dir="$1" owner="${2:-root:root}" mode="${3:-0755}"
  # Only create directory if it doesn't exist, preserving existing permissions
  if [[ ! -d "$dir" ]]; then
    install -d -m "$mode" -o root -g root "$dir"
  fi
  { [[ -L "$dir" ]] || [[ ! -d "$dir" ]]; } && die "Path exists and is not a directory: $dir"
  chown -h "$owner" "$dir" 2>/dev/null || true
  chmod "$mode" "$dir"
}

# ------------------------------------------------------------------------------
# Role detection
# ------------------------------------------------------------------------------
detect_role() {
  if [[ ! -t 0 ]] && (( UNATTENDED == 0 )); then
    warn "No stdin TTY detected; automatically enabling --unattended mode"
    UNATTENDED=1
  fi

  if [[ "$ROLE" != "$ROLE_AUTO" ]]; then
    return 0
  fi

  if (( UNATTENDED == 0 )); then
    local ans=""
    printf '\nThis host can be installed as primary (backup source) or secondary (standby receiver).\n'
    read -r -p "Select role [primary/secondary]: " ans < /dev/tty || true
    case "${ans,,}" in
      p|primary) ROLE="$ROLE_PRIMARY" ;;
      s|secondary) ROLE="$ROLE_SECONDARY" ;;
      *) die "Role is required (primary or secondary)" ;;
    esac
    info "Role selected: $ROLE"
    return 0
  fi

  if [[ -d "$SB_BASE_DIR" || -f "$SB_SYNC_ENV_FILE" ]]; then
    ROLE="$ROLE_SECONDARY"
    info "Detected secondary node (found $SB_BASE_DIR)"
  else
    ROLE="$ROLE_PRIMARY"
    warn "Unattended auto-detect defaulted to primary; pass --role secondary if this is a standby node"
  fi
}

# ------------------------------------------------------------------------------
# Common: base packages for both roles
# ------------------------------------------------------------------------------
install_common() {
  require_root
  check_bash
  detect_pkg_manager

  local -a common=()
  case "$PKG_MANAGER" in
    apt-get)
      common=(bash tar gzip gpg gpg-agent coreutils findutils util-linux gnupg sqlite3 curl python3 python3-venv python3-pip openssh-client)
      ;;
    dnf|yum)
      common=(bash tar gzip gnupg2 coreutils findutils util-linux sqlite curl python3 python3-pip openssh-clients)
      ;;
    *)
      common=(bash tar gzip coreutils findutils util-linux gnupg sqlite3 curl python3)
      ;;
  esac
  install_packages "${common[@]}"

  ensure_dir /etc/x-ui root:root 0700
  ensure_dir /etc/x-ui/standby root:root 0700
}

# ------------------------------------------------------------------------------
# Interactive helpers
# ------------------------------------------------------------------------------
set_env_value() {
  local file="$1" key="$2" value="$3"
  python3 - "$file" "$key" "$value" <<'PY'
from pathlib import Path
import re
import sys

path, key, value = sys.argv[1:]
if re.search(r"[\n\r\x00]", value):
    raise SystemExit(f"Invalid control character in {key}")
text = Path(path).read_text(encoding="utf-8") if Path(path).exists() else ""
pattern = re.compile(rf"^(#\s*)?{re.escape(key)}=.*$", re.M)
line = f"{key}={value}"
if pattern.search(text):
    text = pattern.sub(lambda _: line, text, count=1)
else:
    if text and not text.endswith("\n"):
        text += "\n"
    text += line + "\n"
Path(path).write_text(text, encoding="utf-8")
PY
  chmod 0600 "$file"
}

ask() {
  local prompt="$1" default="${2:-}"
  local ans=""
  if [[ -t 0 || -r /dev/tty ]]; then
    local term_in="/dev/stdin"
    local term_out="/dev/stderr"
    if [[ -r /dev/tty ]]; then
      term_in="/dev/tty"
      term_out="/dev/tty"
    fi
    if [[ -n "$default" ]]; then
      read -r -p "$prompt [$default]: " ans < "$term_in" > "$term_out" 2>&1 || true
      printf '%s' "${ans:-$default}"
    else
      read -r -p "$prompt: " ans < "$term_in" > "$term_out" 2>&1 || true
      printf '%s' "$ans"
    fi
  elif (( UNATTENDED == 1 )); then
    printf '%s' "$default"
  else
    die "Interactive prompt required but no TTY available. Use --unattended or ensure a terminal."
  fi
}

prompt_tg_config() {
  local env_file="$1"
  local token chat_id ans=""
  if (( UNATTENDED == 1 )); then
    warn "Skipping Telegram prompt (--unattended); edit $env_file later"
    return 0
  fi
  info "Telegram alerts (optional, recommended for backup/sync failures)"
  ans="$(ask "Enable Telegram alerts? [Y/n]" "Y")"
  if [[ "$ans" =~ ^[Nn]([Oo])?$ ]]; then
    set_env_value "$env_file" SEND_TELEGRAM 0
    return 0
  fi
  token="$(ask "Telegram bot token")"
  chat_id="$(ask "Telegram chat ID")"
  if [[ ! "$token" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    warn "Invalid TG_BOT_TOKEN; leaving SEND_TELEGRAM=0. Edit $env_file later."
    set_env_value "$env_file" SEND_TELEGRAM 0
    return 0
  fi
  if [[ ! "$chat_id" =~ ^-?[0-9]+$ ]]; then
    warn "Invalid TG_CHAT_ID; leaving SEND_TELEGRAM=0. Edit $env_file later."
    set_env_value "$env_file" SEND_TELEGRAM 0
    return 0
  fi
  set_env_value "$env_file" SEND_TELEGRAM 1
  set_env_value "$env_file" TG_BOT_TOKEN "$token"
  set_env_value "$env_file" TG_CHAT_ID "$chat_id"
  info "Telegram configured in $env_file"
}

gpg_first_fingerprint() {
  local homedir="$1"
  gpg --homedir "$homedir" --batch --with-colons --list-secret-keys 2>/dev/null \
    | awk -F: '$1=="fpr" {print $10; exit}'
}

cleanup_gpg() {
  local homedir="$1"
  if [[ -n "$homedir" ]] && [[ -d "$homedir" ]]; then
    gpgconf --homedir "$homedir" --kill gpg-agent 2>/dev/null || true
  fi
}

generate_gpg_key() {
  local homedir="$1" uid="$2"
  gpg --homedir "$homedir" --batch --pinentry-mode loopback --passphrase '' \
    --quick-generate-key "$uid" default default never
  cleanup_gpg "$homedir"
}

# ------------------------------------------------------------------------------
# Primary role
# ------------------------------------------------------------------------------
install_primary() {
  info "=== Installing PRIMARY role ==="

  local -a req=(basename cat chmod chown cut date df find flock gpg gpgconf gzip install mktemp mv od rm sha256sum sort sqlite3 stat tail tar tr systemctl)
  require_cmd "${req[@]}"

  ensure_dir "$PRIMARY_BACKUP_DIR" root:root 0700
  ensure_dir "${PRIMARY_BACKUP_DIR}/.work" root:root 0700
  ensure_dir "$PRIMARY_GNUPG_DIR" root:root 0700

  # -- Scripts -----------------------------------------------------------------
  local repo_primary="${REPO_ROOT}/primary-node"
  [[ -f "$repo_primary/xui-backup.sh" ]] || die "Cannot find primary-node/xui-backup.sh in repo root: $REPO_ROOT"
  install_file "$repo_primary/xui-backup.sh"  "${PRIMARY_BIN_DIR}/xui-backup"  0755 root:root
  install_file "$repo_primary/xui-restore.sh" "${PRIMARY_BIN_DIR}/xui-restore" 0755 root:root

  # -- Config templates --------------------------------------------------------
  if [[ ! -f "$PRIMARY_ENV_FILE" ]]; then
    install_file "$repo_primary/examples/.env.example" "$PRIMARY_ENV_FILE" 0600 root:root
  else
    info "Preserving existing $PRIMARY_ENV_FILE"
  fi
  prompt_tg_config "$PRIMARY_ENV_FILE"

  if [[ ! -f "$PRIMARY_TRANSFER_ENV_FILE" ]]; then
    install_file "$repo_primary/examples/backup-transfer.env.example" "$PRIMARY_TRANSFER_ENV_FILE" 0600 root:root
  else
    info "Preserving existing $PRIMARY_TRANSFER_ENV_FILE"
  fi

  # -- GPG keyring -------------------------------------------------------------
  local fp=""
  if ! gpg --homedir "$PRIMARY_GNUPG_DIR" --batch --list-secret-keys >/dev/null 2>&1; then
    warn "No secret GPG key in $PRIMARY_GNUPG_DIR"
    if (( UNATTENDED == 0 )); then
      local ans
      ans="$(ask "Generate a primary GPG key now? [Y/n]" "Y")"
      if [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        generate_gpg_key "$PRIMARY_GNUPG_DIR" "xui-backup-primary <backup@primary>"
      fi
    fi
  fi
  fp="$(gpg_first_fingerprint "$PRIMARY_GNUPG_DIR")" || fp=""
  if [[ -n "$fp" ]]; then
    set_env_value "$PRIMARY_ENV_FILE" PRIMARY_LOCAL_RECIPIENT "$fp"
    info "Primary GPG fingerprint written to PRIMARY_LOCAL_RECIPIENT: $fp"
    info "Export this public key for the secondary node:"
    info "  gpg --homedir $PRIMARY_GNUPG_DIR --armor --export $fp > /tmp/primary.pub"
  else
    warn "PRIMARY_LOCAL_RECIPIENT is empty — generate/import a GPG key before first backup"
  fi
  if (( UNATTENDED == 0 )); then
    local secondary_fp
    secondary_fp="$(ask "Secondary GPG fingerprint (40 hex chars, or empty to skip)")"
    if [[ "$secondary_fp" =~ ^[0-9A-Fa-f]{40}$ ]]; then
      set_env_value "$PRIMARY_ENV_FILE" SECONDARY_SYNC_RECIPIENT "$secondary_fp"
    elif [[ -n "$secondary_fp" ]]; then
      warn "Ignoring invalid SECONDARY_SYNC_RECIPIENT; edit $PRIMARY_ENV_FILE later"
    fi
  fi

  # -- Off-site SSH key ----------------------------------------------------------
  if [[ ! -f "$PRIMARY_SSH_KEY" ]]; then
    info "Generating SSH keypair for off-site delivery: $PRIMARY_SSH_KEY"
    ssh-keygen -q -t ed25519 -N "" -f "$PRIMARY_SSH_KEY" -C "xui-backup@primary"
    chown -h root:root "$PRIMARY_SSH_KEY" "$PRIMARY_SSH_KEY.pub"
    chmod 0600 "$PRIMARY_SSH_KEY"
    chmod 0644 "$PRIMARY_SSH_KEY.pub"
  else
    info "SSH key already present: $PRIMARY_SSH_KEY"
  fi
  info "Public key (add to secondary ~xbackup/.ssh/authorized_keys):"
  cat "$PRIMARY_SSH_KEY.pub"

  if [[ ! -f "$PRIMARY_KNOWN_HOSTS" ]]; then
    install -o root -g root -m 0600 /dev/null "$PRIMARY_KNOWN_HOSTS"
    if (( UNATTENDED == 0 )); then
      local secondary_host
      secondary_host="$(ask "Secondary host for ssh-keyscan (hostname/IP, empty to skip)")"
      if [[ -n "$secondary_host" ]]; then
        local keyscan_output
        keyscan_output="$(ssh-keyscan -H "$secondary_host" 2>/dev/null || true)"
        if [[ -n "$keyscan_output" ]]; then
          printf '%s\n' "$keyscan_output" >>"$PRIMARY_KNOWN_HOSTS"
          chmod 0600 "$PRIMARY_KNOWN_HOSTS"
          set_env_value "$PRIMARY_TRANSFER_ENV_FILE" TRANSFER_ENABLED 1
          set_env_value "$PRIMARY_TRANSFER_ENV_FILE" TRANSFER_HOST "$secondary_host"
          set_env_value "$PRIMARY_TRANSFER_ENV_FILE" TRANSFER_USER "$SB_USER"
          set_env_value "$PRIMARY_TRANSFER_ENV_FILE" TRANSFER_KEY "$PRIMARY_SSH_KEY"
          set_env_value "$PRIMARY_TRANSFER_ENV_FILE" TRANSFER_KNOWN_HOSTS "$PRIMARY_KNOWN_HOSTS"
          info "Pinned $secondary_host into $PRIMARY_KNOWN_HOSTS and enabled TRANSFER_ENABLED=1"
        else
          warn "ssh-keyscan returned no keys for $secondary_host; leave TRANSFER_ENABLED=0"
        fi
      else
        warn "No known_hosts yet: ssh-keyscan -H <secondary-host> > $PRIMARY_KNOWN_HOSTS"
      fi
    else
      warn "No known_hosts yet: ssh-keyscan -H <secondary-host> > $PRIMARY_KNOWN_HOSTS"
    fi
  fi

  # -- systemd & logrotate -------------------------------------------------------
  if (( NO_SYSTEMD == 0 )); then
    # Check if systemd is available
    if [[ ! -d /run/systemd/system ]]; then
      warn "systemd not detected (no /run/systemd/system), skipping systemd units"
    else
      write_unit "xui-backup.service" "$(
cat <<EOF
[Unit]
Description=3x-ui Encrypted SQLite Backup + Off-site Delivery
Wants=xui-backup.timer

[Service]
Type=oneshot
ExecStart=${PRIMARY_BIN_DIR}/xui-backup
Nice=19
IOSchedulingClass=idle
RuntimeDirectory=xui-backup
ProtectSystem=strict
ReadWritePaths=${PRIMARY_BACKUP_DIR} /run/xui-backup /var/log
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
)"
      write_unit "xui-backup.timer" "$(
cat <<EOF
[Unit]
Description=Run 3x-ui backup daily at 03:00 UTC
Requires=xui-backup.service

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
RandomizedDelaySec=600
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
)"
      systemctl daemon-reload
      systemctl enable --now xui-backup.timer
      info "Enabled xui-backup.timer"
    fi
  fi

  install_file "$repo_primary/examples/logrotate-xui-backup"  "${LOGROTATE_DIR}/xui-backup"  0644 root:root
  install_file "$repo_primary/examples/logrotate-xui-restore" "${LOGROTATE_DIR}/xui-restore" 0644 root:root

  # -- Placeholder log files -----------------------------------------------------
  if [[ ! -f /var/log/xui-backup.log ]]; then
    install -o root -g root -m 0600 /dev/null /var/log/xui-backup.log
  else
    chmod 0600 /var/log/xui-backup.log || true
  fi
  if [[ ! -f /var/log/xui-restore.log ]]; then
    install -o root -g root -m 0600 /dev/null /var/log/xui-restore.log
  else
    chmod 0600 /var/log/xui-restore.log || true
  fi
  ensure_dir /run/xui-backup root:root 0700
  touch /run/xui-backup/lock
}

# ------------------------------------------------------------------------------
# Secondary role
# ------------------------------------------------------------------------------
create_xbackup_user() {
  if id "$SB_USER" >/dev/null 2>&1; then
    info "User $SB_USER already exists"
    # Ensure shell is correct (idempotency fix)
    local current_shell
    current_shell="$(getent passwd "$SB_USER" | cut -d: -f7)"
    if [[ "$current_shell" != "/bin/bash" ]]; then
      info "Updating shell for $SB_USER from $current_shell to /bin/bash"
      usermod -s /bin/bash "$SB_USER"
    fi
  else
    info "Creating system user $SB_USER"
    useradd -r -s /bin/bash -M -d /home/"$SB_USER" "$SB_USER"
  fi
  mkdir -p /home/"$SB_USER"
  chown -h "$SB_USER:$SB_USER" /home/"$SB_USER"
  chmod 0700 /home/"$SB_USER"
  install -d -m 0700 -o "$SB_USER" -g "$SB_USER" "$SB_SSH_DIR"
  if [[ ! -f "$SB_AUTH_KEYS" ]]; then
    install -o "$SB_USER" -g "$SB_USER" -m 0600 /dev/null "$SB_AUTH_KEYS"
  fi
  chmod 0700 "$SB_SSH_DIR"
  chmod 0600 "$SB_AUTH_KEYS"
}

install_secondary() {
  info "=== Installing SECONDARY role ==="

  local -a req=(basename cat chmod chown cut date df find flock gpg gpgconf gzip install mktemp mv rm sha256sum sort sqlite3 stat tail tar tr systemctl python3)
  require_cmd "${req[@]}"
  python3 - <<'PY' || die "python3 >= 3.10 is required"
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
PY

  create_xbackup_user

  # -- Backup store owned by xbackup -------------------------------------------
  ensure_dir "$SB_BASE_DIR" root:"$SB_USER" 0750
  ensure_dir "$SB_BIN_DIR" root:xbackup 0755
  ensure_dir "$SB_INCOMING_DIR" "$SB_USER:$SB_USER" 0700
  ensure_dir "$SB_INVALID_DIR" "$SB_USER:$SB_USER" 0700
  ensure_dir "$SB_WORK_SYNC_DIR" root:root 0700
  ensure_dir "$SB_SNAPSHOTS_DIR" root:root 0700
  touch "$SB_LOG_FILE"
  chown -h "$SB_USER:$SB_USER" "$SB_LOG_FILE"
  chmod 0600 "$SB_LOG_FILE"
  touch "$SB_LOCK_FILE"
  chown -h "$SB_USER:$SB_USER" "$SB_LOCK_FILE"
  chmod 0600 "$SB_LOCK_FILE"

  # -- Bash scripts ---------------------------------------------------------------
  local repo_secondary="${REPO_ROOT}/secondary-node"
  [[ -d "$repo_secondary" ]] || die "Cannot find secondary-node/ in repo root: $REPO_ROOT"
  install_file "$repo_secondary/xui-backup-receiver.sh"   "${SB_BIN_DIR}/xui-backup-receiver.sh"   0755 "$SB_USER:$SB_USER"
  install_file "$repo_secondary/xui-backup-retention.sh"  "${SB_BIN_DIR}/xui-backup-retention.sh"  0755 "$SB_USER:$SB_USER"
  install_file "$repo_secondary/xui-backup-health.sh"     "${SB_BIN_DIR}/xui-backup-health.sh"     0755 "$SB_USER:$SB_USER"

  ln -sf "${SB_BIN_DIR}/xui-backup-health.sh" "${PRIMARY_BIN_DIR}/xui-backup-health"
  ln -sf "${SB_BIN_DIR}/xui-backup-retention.sh" "${PRIMARY_BIN_DIR}/xui-backup-retention"

  # -- Python sync engine (venv, root) --------------------------------------------
  ensure_dir "$SB_SYNC_DIR" root:root 0700
  local py="${SB_VENV_DIR}/bin/python"
  if [[ ! -x "$py" ]]; then
    info "Creating venv at $SB_VENV_DIR"
    python3 -m venv "$SB_VENV_DIR"
  fi
  info "Installing xui-standby-sync into venv"
  "${SB_VENV_DIR}/bin/pip" install --no-deps --force-reinstall "$repo_secondary/xui_standby_sync" \
    || die "Pip install of xui-standby-sync failed"
  ln -sf "${SB_VENV_DIR}/bin/xui-standby" "${PRIMARY_BIN_DIR}/xui-standby"
  ln -sf "${SB_VENV_DIR}/bin/xui-standby-sync" "${PRIMARY_BIN_DIR}/xui-standby-sync"

  # -- Config & keys ----------------------------------------------------------------
  ensure_dir /etc/x-ui/standby root:root 0700
  if [[ ! -f "$SB_ALLOWLIST_FILE" ]]; then
    install_file "$repo_secondary/examples/allowlist.json.example" "$SB_ALLOWLIST_FILE" 0600 root:root
  else
    info "Preserving existing $SB_ALLOWLIST_FILE"
  fi

  if [[ ! -f "$SB_SYNC_ENV_FILE" ]]; then
    install_file "$repo_secondary/examples/sync.env.example" "$SB_SYNC_ENV_FILE" 0600 root:root
  else
    info "Preserving existing $SB_SYNC_ENV_FILE"
  fi
  prompt_tg_config "$SB_SYNC_ENV_FILE"
  if (( UNATTENDED == 0 )); then
    local primary_ip standby_ip signer_fp
    primary_ip="$(ask "Primary node IP (for stream_settings rewrite, empty to skip)")"
    standby_ip="$(ask "This standby node IP (empty to skip)")"
    signer_fp="$(ask "Trusted primary GPG signer fingerprint (40 hex chars)")"
    [[ -n "$primary_ip" ]] && set_env_value "$SB_SYNC_ENV_FILE" PRIMARY_IP "$primary_ip"
    [[ -n "$standby_ip" ]] && set_env_value "$SB_SYNC_ENV_FILE" STANDBY_IP "$standby_ip"
    if [[ "$signer_fp" =~ ^[0-9A-Fa-f]{40}$ ]]; then
      set_env_value "$SB_SYNC_ENV_FILE" TRUSTED_GPG_SIGNER_FINGERPRINTS "$signer_fp"
    elif [[ -n "$signer_fp" ]]; then
      warn "Ignoring invalid TRUSTED_GPG_SIGNER_FINGERPRINTS; edit $SB_SYNC_ENV_FILE later"
    fi
  else
    warn "Set TRUSTED_GPG_SIGNER_FINGERPRINTS and PRIMARY_IP/STANDBY_IP in $SB_SYNC_ENV_FILE"
  fi

  ensure_dir "$SB_GNUPG_DIR" root:root 0700
  if ! gpg --homedir "$SB_GNUPG_DIR" --batch --list-secret-keys >/dev/null 2>&1; then
    warn "No secret GPG key in $SB_GNUPG_DIR (needed to decrypt incoming archives)"
    if (( UNATTENDED == 0 )); then
      local ans
      ans="$(ask "Generate a secondary GPG key now? [Y/n]" "Y")"
      if [[ "$ans" =~ ^[Yy]([Ee][Ss])?$ ]]; then
        generate_gpg_key "$SB_GNUPG_DIR" "xui-backup-secondary <backup@secondary>"
      fi
    fi
  fi
  local fp
  fp="$(gpg_first_fingerprint "$SB_GNUPG_DIR")" || fp=""
  [[ -n "$fp" ]] && info "Secondary GPG key fingerprint: $fp"
  info "Import the PRIMARY public key into $SB_GNUPG_DIR:"
  info "  gpg --homedir $SB_GNUPG_DIR --import /tmp/primary.pub"

  # -- Standby mode marker ------------------------------------------------------------
  if [[ ! -f "$SB_STANDBY_MODE_FILE" ]]; then
    printf 'STANDBY\n' >"$SB_STANDBY_MODE_FILE"
    chown -h root:root "$SB_STANDBY_MODE_FILE"
    chmod 0600 "$SB_STANDBY_MODE_FILE"
  fi

  if [[ ! -f /var/log/xui-standby-sync.log ]]; then
    install -o root -g root -m 0600 /dev/null /var/log/xui-standby-sync.log
  else
    chmod 0600 /var/log/xui-standby-sync.log || true
  fi

  # -- systemd ------------------------------------------------------------------------
  if (( NO_SYSTEMD == 0 )); then
    # Check if systemd is available
    if [[ ! -d /run/systemd/system ]]; then
      warn "systemd not detected (no /run/systemd/system), skipping systemd units"
    else
      write_unit "xui-standby-sync.service" "$(
cat <<EOF
[Unit]
Description=3x-ui Standby Sync Engine
Wants=xui-standby-sync.timer

[Service]
Type=oneshot
ExecStart=${PRIMARY_BIN_DIR}/xui-standby sync --json
TimeoutStartSec=600
Nice=19
IOSchedulingClass=idle
ProtectSystem=strict
ReadWritePaths=/etc/x-ui /opt/xui-backups /opt/xui-standby /run /var/log
ProtectHome=true

[Install]
WantedBy=multi-user.target
EOF
)"
      write_unit "xui-standby-sync.timer" "$(
cat <<EOF
[Unit]
Description=Run 3x-ui standby sync every 2 hours
Requires=xui-standby-sync.service

[Timer]
OnCalendar=*-*-* 00/2:00:00 UTC
RandomizedDelaySec=300
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
)"
      write_unit "xui-backup-retention.service" "$(
cat <<EOF
[Unit]
Description=3x-ui Backup Retention Cleanup

[Service]
Type=oneshot
User=${SB_USER}
Group=${SB_USER}
ExecStart=${PRIMARY_BIN_DIR}/xui-backup-retention
Nice=19
IOSchedulingClass=idle

[Install]
WantedBy=multi-user.target
EOF
)"
      write_unit "xui-backup-retention.timer" "$(
cat <<EOF
[Unit]
Description=Run 3x-ui backup retention daily at 04:00 UTC
Requires=xui-backup-retention.service

[Timer]
OnCalendar=*-*-* 04:00:00 UTC
RandomizedDelaySec=300
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF
)"
      systemctl daemon-reload
      systemctl enable --now xui-standby-sync.timer xui-backup-retention.timer
      info "Enabled xui-standby-sync.timer and xui-backup-retention.timer"
    fi
  fi

  info "SSH receiver: append the primary public key to $SB_AUTH_KEYS"
  info "Required options: restrict,no-pty,no-port-forwarding,no-X11-forwarding,command=\"${SB_BIN_DIR}/xui-backup-receiver.sh\""
  info "See example: ${repo_secondary}/examples/authorized_keys.example"

  # -- logrotate ---------------------------------------------------------------------
  install_file "$repo_secondary/examples/logrotate-xui-standby-sync" "${LOGROTATE_DIR}/xui-standby-sync" 0644 root:root
  install_file "$repo_secondary/examples/logrotate-xui-backup-receiver" "${LOGROTATE_DIR}/xui-backup-receiver" 0644 root:root
}

# ------------------------------------------------------------------------------
# Post-install summary
# ------------------------------------------------------------------------------
print_summary() {
  echo
  echo "==================== INSTALL SUMMARY ===================="
  if [[ "$ROLE" == "$ROLE_PRIMARY" ]]; then
    echo "Role: primary"
    echo "  Backup CLI : ${PRIMARY_BIN_DIR}/xui-backup"
    echo "  Restore CLI: ${PRIMARY_BIN_DIR}/xui-restore"
    echo "  GPG home   : ${PRIMARY_GNUPG_DIR}"
    echo "  SSH key    : ${PRIMARY_SSH_KEY}"
    echo "  Env        : ${PRIMARY_ENV_FILE}"
    echo "  Transfer   : ${PRIMARY_TRANSFER_ENV_FILE}"
    echo "  Timer      : systemctl list-timers xui-backup.timer"
    echo "  Test run   : ${PRIMARY_BIN_DIR}/xui-backup --dry-run"
    echo "  Next       : import secondary GPG pubkey, then fill SECONDARY_SYNC_RECIPIENT"
  else
    echo "Role: secondary"
    echo "  Receiver   : ${SB_BIN_DIR}/xui-backup-receiver.sh (via ssh forced command)"
    echo "  Retention  : ${PRIMARY_BIN_DIR}/xui-backup-retention"
    echo "  Health     : ${PRIMARY_BIN_DIR}/xui-backup-health"
    echo "  Sync engine: ${PRIMARY_BIN_DIR}/xui-standby (venv at ${SB_VENV_DIR})"
    echo "  Sync env   : ${SB_SYNC_ENV_FILE}"
    echo "  Allowlist  : ${SB_ALLOWLIST_FILE}"
    echo "  SSH keys   : ${SB_AUTH_KEYS}"
    echo "  Snapshots  : ${SB_SNAPSHOTS_DIR}"
    echo "  Timers     : xui-standby-sync.timer, xui-backup-retention.timer"
    echo "  Validate   : ${PRIMARY_BIN_DIR}/xui-standby validate --json"
    echo "  Next       : import primary GPG pubkey and paste SSH pubkey into authorized_keys"
  fi
  echo "========================================================="
}

# ------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------
main() {
  parse_args "$@"
  REPO_ROOT="$(cd "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  check_bash
  require_root
  detect_role
  install_common

  if [[ "$ROLE" == "$ROLE_PRIMARY" ]]; then
    install_primary
  else
    install_secondary
  fi

  print_summary
  return 0
}

main "$@"