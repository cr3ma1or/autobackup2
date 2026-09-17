#!/usr/bin/env bash
# xui-failover: defensive L4 transit and failover controller for a 3x-ui standby.

set -Eeuo pipefail
umask 077

readonly SCRIPT_VERSION="1.1.0"
readonly CONFIG_FILE="/etc/x-ui/sync.env"
readonly LOCK_FILE="/run/xui-failover.lock"
readonly FAILOVER_GUARD_FILE="/run/xui-standby.lock"
readonly SYNC_LOCK_FILE="/run/xui-standby-sync.lock"
readonly TRANSIT_CHAIN="XUI_TRANSIT_DNAT"
readonly SYNC_TIMER="xui-standby-sync.timer"
readonly BACKUP_TIMER="xui-backup.timer"
readonly XUI_SERVICE="x-ui.service"

PRIMARY_IP=""
TRANSIT_PORT_MAP=""
STANDBY_MODE_FILE="/etc/x-ui/standby-mode"
TG_BOT_TOKEN=""
TG_CHAT_ID=""
TG_PROXY_URL=""
SEND_TELEGRAM="0"
LOCK_HELD=0
SYNC_LOCK_HELD=0
FAILOVER_GUARD_CREATED=0
KEEP_FAILOVER_GUARD=0
SENSITIVE_FILES=()

log() {
  local level="$1" message="$2"
  printf '%s [%s] [xui-failover] %s\n' \
    "$(date -u '+%Y-%m-%d %H:%M:%S UTC')" "$level" "$message" >&2
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  local sensitive_file
  for sensitive_file in "${SENSITIVE_FILES[@]}"; do
    if [[ -f "$sensitive_file" ]]; then
      shred -u -z -n 1 -- "$sensitive_file" 2>/dev/null || rm -f -- "$sensitive_file"
    fi
  done
  if ((FAILOVER_GUARD_CREATED == 1 && KEEP_FAILOVER_GUARD == 0)); then
    rm -f -- "$FAILOVER_GUARD_FILE"
  fi
  if ((SYNC_LOCK_HELD == 1)); then
    flock -u 8 2>/dev/null || true
    exec 8>&- 2>/dev/null || true
  fi
  if ((LOCK_HELD == 1)); then
    flock -u 9 2>/dev/null || true
    exec 9>&- 2>/dev/null || true
  fi
  unset TG_BOT_TOKEN TG_CHAT_ID TG_PROXY_URL
  exit "$rc"
}

on_error() {
  local rc="$1" line="$2" command="$3"
  trap - ERR
  log ERROR "unexpected failure; line=${line}; exit=${rc}; command=${command}"
  exit "$rc"
}

on_int() {
  trap - INT
  log WARN "interrupted by SIGINT"
  exit 130
}

on_term() {
  trap - TERM
  log WARN "terminated by SIGTERM"
  exit 143
}

trap cleanup EXIT
trap 'on_error "$?" "$LINENO" "$BASH_COMMAND"' ERR
trap on_int INT
trap on_term TERM

usage() {
  cat <<'EOF'
Usage:
  xui-failover status [--json]
  xui-failover promote
  xui-failover standby [--yes]
  xui-failover --help
  xui-failover --version
EOF
}

fail() {
  log ERROR "$1"
  exit 1
}

require_root() {
  ((EUID == 0)) || fail "root privileges are required"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    log ERROR "required command not found: $1"
    exit 127
  }
}

acquire_lock() {
  if [[ -L "$LOCK_FILE" ]]; then
    fail "refusing symlink lock file: $LOCK_FILE"
  fi
  exec 9>"$LOCK_FILE"
  chmod 0600 -- "$LOCK_FILE"
  if ! flock -n 9; then
    fail "another xui-failover process is already running"
  fi
  LOCK_HELD=1
}

create_failover_guard() {
  if [[ -L "$FAILOVER_GUARD_FILE" ]]; then
    fail "refusing symlink failover guard: $FAILOVER_GUARD_FILE"
  fi
  if [[ ! -e "$FAILOVER_GUARD_FILE" ]]; then
    install -m 0600 -o root -g root /dev/null "$FAILOVER_GUARD_FILE"
    FAILOVER_GUARD_CREATED=1
  fi
  [[ -f "$FAILOVER_GUARD_FILE" ]] || fail "failover guard is not a regular file"
  [[ "$(stat -c '%U:%G:%a' -- "$FAILOVER_GUARD_FILE")" == "root:root:600" ]] || \
    fail "unsafe failover guard owner or permissions"
}

acquire_sync_exclusion() {
  if [[ -L "$SYNC_LOCK_FILE" ]]; then
    fail "refusing symlink synchronization lock: $SYNC_LOCK_FILE"
  fi
  exec 8>"$SYNC_LOCK_FILE"
  chmod 0600 -- "$SYNC_LOCK_FILE"
  if ! flock -n 8; then
    fail "synchronization is active; failover transition aborted"
  fi
  SYNC_LOCK_HELD=1
}

release_sync_exclusion() {
  ((SYNC_LOCK_HELD == 1)) || return 0
  flock -u 8
  exec 8>&-
  SYNC_LOCK_HELD=0
}

set_config_value() {
  local key="$1" value="$2"
  case "$key" in
    PRIMARY_IP) PRIMARY_IP="$value" ;;
    TRANSIT_PORT_MAP) TRANSIT_PORT_MAP="$value" ;;
    STANDBY_MODE_FILE) STANDBY_MODE_FILE="$value" ;;
    SEND_TELEGRAM) SEND_TELEGRAM="$value" ;;
    TG_BOT_TOKEN) TG_BOT_TOKEN="$value" ;;
    TG_CHAT_ID) TG_CHAT_ID="$value" ;;
    TG_PROXY_URL) TG_PROXY_URL="$value" ;;
  esac
}

load_config() {
  local owner mode raw_line line key value quote
  local -A seen_keys=()

  [[ -e "$CONFIG_FILE" ]] || fail "configuration file is missing: $CONFIG_FILE"
  [[ ! -L "$CONFIG_FILE" && -f "$CONFIG_FILE" ]] || \
    fail "configuration must be a regular non-symlink file: $CONFIG_FILE"

  owner="$(stat -c '%U:%G' -- "$CONFIG_FILE")"
  mode="$(stat -c '%a' -- "$CONFIG_FILE")"
  [[ "$owner" == "root:root" ]] || fail "unsafe configuration owner: $owner (expected root:root)"
  [[ "$mode" =~ ^[46]00$ ]] || \
    fail "unsafe configuration permissions: $mode (expected 0400 or 0600)"

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    [[ "$raw_line" != *$'\r'* ]] || fail "configuration contains a carriage return"
    line="${raw_line#"${raw_line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" && "${line:0:1}" != "#" ]] || continue
    [[ "$line" =~ ^(export[[:space:]]+)?([A-Z][A-Z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$ ]] || \
      fail "malformed configuration entry"
    key="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    value="${value%"${value##*[![:space:]]}"}"
    if [[ "${value:0:1}" == "\"" || "${value:0:1}" == "'" ]]; then
      quote="${value:0:1}"
      [[ ${#value} -ge 2 && "${value: -1}" == "$quote" ]] || \
        fail "unbalanced quotes for configuration key: $key"
      value="${value:1:${#value}-2}"
    elif [[ "$value" == *[[:space:]\`\$\\]* ]]; then
      fail "unsafe unquoted value for configuration key: $key"
    fi
    case "$key" in
      PRIMARY_IP | TRANSIT_PORT_MAP | STANDBY_MODE_FILE | SEND_TELEGRAM | \
        TG_BOT_TOKEN | TG_CHAT_ID | TG_PROXY_URL)
        [[ ! -v "seen_keys[$key]" ]] || fail "duplicate configuration key: $key"
        seen_keys["$key"]=1
        set_config_value "$key" "$value"
        ;;
    esac
  done <"$CONFIG_FILE"

  [[ -n "${PRIMARY_IP:-}" ]] || fail "configuration error: PRIMARY_IP is required"
  [[ -n "${TRANSIT_PORT_MAP:-}" ]] || fail "configuration error: TRANSIT_PORT_MAP is required"
  STANDBY_MODE_FILE="${STANDBY_MODE_FILE:-/etc/x-ui/standby-mode}"
  SEND_TELEGRAM="${SEND_TELEGRAM:-0}"
  TG_BOT_TOKEN="${TG_BOT_TOKEN:-}"
  TG_CHAT_ID="${TG_CHAT_ID:-}"
  TG_PROXY_URL="${TG_PROXY_URL:-}"

  validate_config
}

validate_ipv4() {
  local address="$1" octet
  local -a octets
  [[ "$address" =~ ^[0-9]+(\.[0-9]+){3}$ ]] || return 1
  IFS='.' read -r -a octets <<<"$address"
  ((${#octets[@]} == 4)) || return 1
  for octet in "${octets[@]}"; do
    [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ ]] || return 1
    ((10#$octet <= 255)) || return 1
  done
}

validate_port_map() {
  local pair external_port internal_port
  local -a mappings

  read -r -a mappings <<<"$TRANSIT_PORT_MAP"
  ((${#mappings[@]} > 0)) || return 1
  for pair in "${mappings[@]}"; do
    [[ "$pair" =~ ^([0-9]+):([0-9]+)$ ]] || return 1
    external_port="${BASH_REMATCH[1]}"
    internal_port="${BASH_REMATCH[2]}"
    ((10#$external_port >= 1 && 10#$external_port <= 65535)) || return 1
    ((10#$internal_port >= 1 && 10#$internal_port <= 65535)) || return 1
  done
}

validate_config() {
  validate_ipv4 "$PRIMARY_IP" || fail "configuration error: PRIMARY_IP must be a valid IPv4 address"
  validate_port_map || \
    fail "configuration error: TRANSIT_PORT_MAP must contain space-separated ext_port:int_port pairs"
  [[ "$STANDBY_MODE_FILE" == /* && "$STANDBY_MODE_FILE" != *$'\n'* ]] || \
    fail "configuration error: STANDBY_MODE_FILE must be an absolute path"
  [[ "$SEND_TELEGRAM" == "0" || "$SEND_TELEGRAM" == "1" ]] || \
    fail "configuration error: SEND_TELEGRAM must be 0 or 1"
  if [[ "$SEND_TELEGRAM" == "1" ]]; then
    [[ "$TG_BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] || \
      fail "configuration error: TG_BOT_TOKEN is missing or malformed"
    [[ -n "$TG_CHAT_ID" && "$TG_CHAT_ID" != *$'\n'* ]] || \
      fail "configuration error: TG_CHAT_ID is required"
    if [[ -n "$TG_PROXY_URL" ]]; then
      [[ "$TG_PROXY_URL" =~ ^(socks5h?|https?)://[^[:space:]\"\\]+$ ]] || \
        fail "configuration error: TG_PROXY_URL is malformed"
    fi
    require_command curl
    require_command shred
  fi
}

ensure_nat_scaffold() {
  iptables -w -t nat -N "$TRANSIT_CHAIN" 2>/dev/null || true
  while iptables -w -t nat -C PREROUTING -j "$TRANSIT_CHAIN" 2>/dev/null; do
    iptables -w -t nat -D PREROUTING -j "$TRANSIT_CHAIN"
  done
  iptables -w -t nat -I PREROUTING 1 -j "$TRANSIT_CHAIN"
  iptables -w -t nat -C POSTROUTING -m conntrack --ctstate DNAT \
    -d "$PRIMARY_IP" -j MASQUERADE 2>/dev/null || \
    iptables -w -t nat -A POSTROUTING -m conntrack --ctstate DNAT \
      -d "$PRIMARY_IP" -j MASQUERADE
}

flush_transit_chain() {
  ensure_nat_scaffold
  iptables -w -t nat -F "$TRANSIT_CHAIN"
}

populate_transit_chain() {
  local pair external_port internal_port
  local -a mappings

  ensure_nat_scaffold
  iptables -w -t nat -F "$TRANSIT_CHAIN"
  read -r -a mappings <<<"$TRANSIT_PORT_MAP"
  for pair in "${mappings[@]}"; do
    external_port="${pair%%:*}"
    internal_port="${pair##*:}"
    if ! iptables -w -t nat -A "$TRANSIT_CHAIN" -p tcp \
      --dport "$external_port" -j DNAT --to-destination "${PRIMARY_IP}:${internal_port}"; then
      iptables -w -t nat -F "$TRANSIT_CHAIN" || true
      fail "failed to install transit mapping; chain was left empty"
    fi
  done
}

save_firewall() {
  local rules_dir="/etc/iptables" rules_file="/etc/iptables/rules.v4" temp_file

  if command -v netfilter-persistent >/dev/null 2>&1; then
    netfilter-persistent save
    return
  fi

  require_command iptables-save
  install -d -m 0700 -o root -g root -- "$rules_dir"
  [[ ! -L "$rules_file" ]] || fail "refusing symlink firewall rules file: $rules_file"
  temp_file="$(mktemp "${rules_dir}/.rules.v4.XXXXXX")"
  if ! iptables-save >"$temp_file"; then
    rm -f -- "$temp_file"
    fail "iptables-save failed"
  fi
  chmod 0600 -- "$temp_file"
  mv -f -- "$temp_file" "$rules_file"
}

write_mode() {
  local mode="$1" marker_dir temp_file
  marker_dir="$(dirname -- "$STANDBY_MODE_FILE")"
  [[ -d "$marker_dir" ]] || fail "marker directory does not exist: $marker_dir"
  [[ ! -L "$STANDBY_MODE_FILE" ]] || fail "refusing symlink marker file: $STANDBY_MODE_FILE"
  temp_file="$(mktemp "${marker_dir}/.standby-mode.XXXXXX")"
  printf '%s\n' "$mode" >"$temp_file"
  chmod 0644 -- "$temp_file"
  chown root:root -- "$temp_file"
  mv -f -- "$temp_file" "$STANDBY_MODE_FILE"
}

send_telegram() {
  local message="$1" work_dir curl_config chat_file message_file
  [[ "$SEND_TELEGRAM" == "1" ]] || return 0

  work_dir="$(dirname -- "$STANDBY_MODE_FILE")"
  curl_config="$(mktemp "${work_dir}/.failover-curl.XXXXXX")"
  chat_file="$(mktemp "${work_dir}/.failover-chat.XXXXXX")"
  message_file="$(mktemp "${work_dir}/.failover-message.XXXXXX")"
  SENSITIVE_FILES+=("$curl_config" "$chat_file" "$message_file")
  printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TG_BOT_TOKEN" >"$curl_config"
  if [[ -n "$TG_PROXY_URL" ]]; then
    printf 'proxy = "%s"\n' "$TG_PROXY_URL" >>"$curl_config"
  fi
  printf '%s' "$TG_CHAT_ID" >"$chat_file"
  printf '%s' "$message" >"$message_file"
  chmod 0600 -- "$curl_config" "$chat_file" "$message_file"

  if ! curl --config "$curl_config" --silent --show-error --fail --max-time 15 \
    --data-urlencode "chat_id@${chat_file}" \
    --data-urlencode "text@${message_file}" >/dev/null; then
    log WARN "Telegram notification failed"
  fi
  shred -u -z -n 1 -- "$curl_config" "$chat_file" "$message_file"
  SENSITIVE_FILES=()
  return 0
}

timer_state() {
  local unit="$1" active enabled
  active="$(systemctl is-active "$unit" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  printf '%s/%s' "${active:-unknown}" "${enabled:-unknown}"
}

read_mode() {
  local mode="UNKNOWN"
  if [[ -f "$STANDBY_MODE_FILE" && ! -L "$STANDBY_MODE_FILE" ]]; then
    IFS= read -r mode <"$STANDBY_MODE_FILE" || true
  fi
  case "$mode" in
    STANDBY | PROMOTED) printf '%s' "$mode" ;;
    *) printf 'UNKNOWN' ;;
  esac
}

transit_stats() {
  local output
  if ! output="$(iptables -w -t nat -vnxL "$TRANSIT_CHAIN" 2>/dev/null)"; then
    printf '0 0'
    return
  fi
  awk '$3 == "DNAT" { rules++; bytes += $2 } END { printf "%d %.0f", rules + 0, bytes + 0 }' <<<"$output"
}

json_escape() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '%s' "$value"
}

status_command() {
  local json="$1" mode stats rule_count traffic_bytes sync_state backup_state
  mode="$(read_mode)"
  stats="$(transit_stats)"
  read -r rule_count traffic_bytes <<<"$stats"
  sync_state="$(timer_state "$SYNC_TIMER")"
  backup_state="$(timer_state "$BACKUP_TIMER")"

  if ((json == 1)); then
    printf '{"mode":"%s","transit":{"chain":"%s","active_rules":%s,"traffic_bytes":%s},"timers":{"%s":"%s","%s":"%s"}}\n' \
      "$(json_escape "$mode")" "$(json_escape "$TRANSIT_CHAIN")" "$rule_count" "$traffic_bytes" \
      "$(json_escape "$SYNC_TIMER")" "$(json_escape "$sync_state")" \
      "$(json_escape "$BACKUP_TIMER")" "$(json_escape "$backup_state")"
  else
    printf 'Mode: %s\n' "$mode"
    printf 'Transit chain: %s\n' "$TRANSIT_CHAIN"
    printf 'Active DNAT rules: %s\n' "$rule_count"
    printf 'Transit traffic: %s bytes\n' "$traffic_bytes"
    printf '%s: %s\n' "$SYNC_TIMER" "$sync_state"
    printf '%s: %s\n' "$BACKUP_TIMER" "$backup_state"
  fi
}

promote_command() {
  create_failover_guard
  acquire_sync_exclusion
  systemctl stop "$SYNC_TIMER"
  systemctl stop xui-standby-sync.service
  flush_transit_chain
  save_firewall
  write_mode PROMOTED
  KEEP_FAILOVER_GUARD=1
  systemctl enable --now "$BACKUP_TIMER"
  systemctl restart "$XUI_SERVICE"
  send_telegram "🚨 #PROMOTED: Secondary node is ACTIVE
Host: $(hostname -f 2>/dev/null || hostname)
Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  log INFO "node promoted; local backup timer enabled"
}

confirm_standby() {
  local answer
  [[ -t 0 ]] || fail "Interactive prompt required, but no TTY is attached. Aborting for safety."
  printf 'Type CONFIRM_STANDBY to return this node to standby: ' >&2
  IFS= read -r answer
  [[ "$answer" == "CONFIRM_STANDBY" ]] || fail "standby transition was not confirmed"
}

standby_command() {
  local assume_yes="$1"
  ((assume_yes == 1)) || confirm_standby

  create_failover_guard
  acquire_sync_exclusion
  systemctl stop "$BACKUP_TIMER"
  systemctl disable "$BACKUP_TIMER"
  populate_transit_chain
  save_firewall
  write_mode STANDBY
  rm -f -- "$FAILOVER_GUARD_FILE"
  FAILOVER_GUARD_CREATED=0
  release_sync_exclusion
  systemctl enable --now "$SYNC_TIMER"
  systemctl restart "$XUI_SERVICE"
  send_telegram "#INFO: Secondary node returned to STANDBY
Host: $(hostname -f 2>/dev/null || hostname)
Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  log INFO "node returned to standby successfully"
}

main() {
  local command="${1:-}" json=0 assume_yes=0 argument

  case "$command" in
    -h | --help) usage; return 0 ;;
    -v | --version) printf 'xui-failover %s\n' "$SCRIPT_VERSION"; return 0 ;;
    status | promote | standby) shift ;;
    "") usage >&2; return 1 ;;
    *) fail "unknown command: $command" ;;
  esac

  for argument in "$@"; do
    case "$command:$argument" in
      status:--json) json=1 ;;
      standby:--yes) assume_yes=1 ;;
      *) fail "unsupported argument for $command: $argument" ;;
    esac
  done

  require_root
  for argument in date dirname flock hostname install iptables mktemp mv stat systemctl awk; do
    require_command "$argument"
  done
  load_config

  if [[ "$command" != "status" ]]; then
    acquire_lock
  fi

  case "$command" in
    status) status_command "$json" ;;
    promote) promote_command ;;
    standby) standby_command "$assume_yes" ;;
  esac
}

main "$@"
