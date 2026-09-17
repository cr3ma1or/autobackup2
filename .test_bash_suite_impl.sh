#!/usr/bin/env bash
# ==============================================================================
# Private implementation for test_bash_suite.sh.  The public runner execs this
# file so the suite can be updated atomically without mutating host paths.
# ==============================================================================
set -Eeuo pipefail
umask 077

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SELF="$ROOT_DIR/test_bash_suite.sh"
readonly C_RESET=$'\033[0m'
readonly C_RED=$'\033[0;31m'
readonly C_GREEN=$'\033[0;32m'
readonly C_YELLOW=$'\033[0;33m'
readonly C_BLUE=$'\033[0;34m'

step() { printf '%s==>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
pass() { printf '%s[PASS]%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
die() { printf '%s[FAIL]%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
assert_file() { [[ -f "$1" ]] || die "$2 ($1)"; }
assert_contains() { grep -Fq -- "$2" "$1" || die "$3"; }
run_expect_failure() {
  local description="$1"; shift
  local output rc
  output="$(mktemp "$SANDBOX_BASE/${description//[^A-Za-z0-9]/_}.XXXXXX")"
  set +e
  "$@" >"$output" 2>&1
  rc=$?
  set -e
  (( rc != 0 )) || { cat "$output" >&2; die "$description: ожидалась ошибка, получен rc=0"; }
  printf '%s' "$output"
}

if [[ "${XUI_BASH_SUITE_ISOLATED:-0}" != 1 ]]; then
  if (( EUID != 0 )); then
    exec sudo --preserve-env=PATH unshare -m -n --propagation private \
      env XUI_BASH_SUITE_ISOLATED=1 bash "$SELF" "$@"
  fi
  exec unshare -m -n --propagation private env XUI_BASH_SUITE_ISOLATED=1 bash "$SELF" "$@"
fi

SANDBOX_BASE="$(mktemp -d /tmp/xui-bash-suite.XXXXXX)"
readonly SANDBOX_BASE
readonly MOCK_BIN="$SANDBOX_BASE/mock-bin"
readonly MOCK_LOG="$SANDBOX_BASE/mock.log"
readonly IPTABLES_STATE="$SANDBOX_BASE/iptables.rules"
readonly MOUNTS=(/opt/xui-backups /etc/x-ui /backup/x-ui /var/log /run/xui-backup)

cleanup() {
  local rc=$?
  trap - EXIT ERR INT TERM
  printf '\n%s[CLEANUP]%s removing isolated mounts and sandbox\n' "$C_BLUE" "$C_RESET" >&2
  local mountpoint
  for mountpoint in "${MOUNTS[@]}"; do
    mountpoint -q -- "$mountpoint" && umount -l -- "$mountpoint" || true
  done
  rm -rf -- "$SANDBOX_BASE"
  exit "$rc"
}
on_error() { local rc=$?; trap - ERR; printf '%s[ERROR]%s line=%s command=%q rc=%s\n' "$C_RED" "$C_RESET" "$1" "$2" "$rc" >&2; exit "$rc"; }
on_int() { exit 130; }
on_term() { exit 143; }
trap cleanup EXIT
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR
trap on_int INT
trap on_term TERM

make_mock() {
  local name="$1"
  cat >"$MOCK_BIN/$name"
  chmod 0755 -- "$MOCK_BIN/$name"
}

setup_mocks() {
  mkdir -p -- "$MOCK_BIN"
  chmod 0755 -- "$SANDBOX_BASE" "$MOCK_BIN"
  : >"$MOCK_LOG"; chmod 0666 -- "$MOCK_LOG"

  make_mock systemctl <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >>"${MOCK_LOG_FILE:?}"
case "${1:-}" in is-active|is-enabled) printf 'inactive\n'; exit 3;; esac
exit 0
EOF
  make_mock netfilter-persistent <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'netfilter-persistent %s\n' "$*" >>"${MOCK_LOG_FILE:?}"
EOF
  make_mock iptables <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
state="${IPTABLES_STATE:?}"; chain="XUI_TRANSIT_DNAT"
args=" $* "
printf 'iptables %s\n' "$*" >>"${MOCK_LOG_FILE:?}"
if [[ "$args" == *" -S $chain "* ]]; then
  [[ -f "$state" ]] && cat "$state"; exit 0
fi
if [[ "$args" == *" -vnxL $chain "* ]]; then
  [[ -f "$state" ]] && sed 's/^-A /0 0 DNAT /' "$state"; exit 0
fi
if [[ "$args" == *" -N $chain "* ]]; then touch "$state"; exit 0; fi
if [[ "$args" == *' -C '* ]]; then exit 1; fi
if [[ "$args" == *" -F $chain "* ]]; then : >"$state"; exit 0; fi
if [[ "$args" == *" -A $chain "* ]]; then
  port=""; target=""
  while (($#)); do case "$1" in --dport) port="$2"; shift 2;; --to-destination) target="$2"; shift 2;; *) shift;; esac; done
  printf '%s\n' "-A $chain -p tcp --dport $port -j DNAT --to-destination $target" >>"$state"; exit 0
fi
exit 0
EOF
  make_mock gpg <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
if [[ " $* " == *' --decrypt '* ]]; then
  output=''; input=''
  while (($#)); do
    case "$1" in
      --status-fd)
        status_fd="$2"; shift 2
        ;;
      --output)
        output="$2"; shift 2
        ;;
      *)
        input="$1"; shift
        ;;
    esac
  done
  cp -- "$input" "$output"
  printf '[GNUPG:] GOODSIG 0123456789012345678901234567890123456789 test-signer\n' >&"$status_fd"
  printf '[GNUPG:] VALIDSIG 0123456789012345678901234567890123456789 20260917 0 4 0 1 10 00 0123456789012345678901234567890123456789\n' >&"$status_fd"
  exit 0
fi
exit 0
EOF
  export MOCK_LOG_FILE="$MOCK_LOG" IPTABLES_STATE
  export PATH="$MOCK_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
}

setup_filesystems() {
  command -v sqlite3 >/dev/null || die 'sqlite3 обязателен для проверки настоящего SQLite payload'
  command -v tar >/dev/null || die 'tar обязателен для проверки payload'
  ip link set lo up
  ip link show lo | grep -q 'UP' || die 'loopback не поднят в network namespace'

  mkdir -p /opt/xui-backups /etc/x-ui /backup/x-ui /var/log /run/xui-backup
  mount -t tmpfs -o mode=0755 tmpfs-opt /opt/xui-backups
  mount -t tmpfs -o mode=0755 tmpfs-etc /etc/x-ui
  mount -t tmpfs -o mode=0700 tmpfs-backup /backup/x-ui
  mount -t tmpfs -o mode=0755 tmpfs-log /var/log
  mount -t tmpfs -o mode=0700 tmpfs-run /run/xui-backup

  id -u xbackup >/dev/null 2>&1 || useradd -r -M -s /usr/sbin/nologin xbackup
  mkdir -p /opt/xui-backups/{incoming,invalid,bin}
  chown root:root /opt/xui-backups /opt/xui-backups/bin
  chmod 0755 /opt/xui-backups /opt/xui-backups/bin
  chown xbackup:xbackup /opt/xui-backups/incoming /opt/xui-backups/invalid
  chmod 0700 /opt/xui-backups/incoming /opt/xui-backups/invalid
  install -o xbackup -g xbackup -m 0600 /dev/null /opt/xui-backups/.store.lock
  install -o xbackup -g xbackup -m 0600 /dev/null /opt/xui-backups/receiver.log
  install -d -o root -g root -m 0700 /etc/x-ui/standby/primary-local-gnupg
  install -o root -g root -m 0600 /dev/null /etc/x-ui/x-ui.db
  install -d -o root -g root -m 0700 /backup/x-ui/.work

  install -m 0755 "$ROOT_DIR/secondary-node/xui-backup-receiver.sh" /opt/xui-backups/bin/xui-backup-receiver.sh
  install -m 0755 "$ROOT_DIR/secondary-node/xui-backup-retention.sh" /opt/xui-backups/bin/xui-backup-retention.sh
  install -m 0755 "$ROOT_DIR/secondary-node/xui-backup-health.sh" /opt/xui-backups/bin/xui-backup-health.sh
  install -m 0755 "$ROOT_DIR/secondary-node/xui-failover.sh" /opt/xui-backups/bin/xui-failover.sh
}

receiver() {
  local command="$1" payload="$2" output="$3"
  printf '%s' "$payload" | su -s /bin/bash xbackup -c "PATH='$PATH' MOCK_LOG_FILE='$MOCK_LOG' IPTABLES_STATE='$IPTABLES_STATE' SSH_ORIGINAL_COMMAND='$command' /opt/xui-backups/bin/xui-backup-receiver.sh" >"$output" 2>&1
}

scenario_receiver_and_security() {
  step 'receiver: verified delivery, protocol injection and immediate lock contention'
  local payload='payload-for-receiver' name hash size output rc lock_pid
  name='xui-backup-20260917T120000Z-99999-111111111.tar.gz.gpg'
  hash="$(printf %s "$payload" | sha256sum | awk '{print $1}')"; size=${#payload}
  output="$SANDBOX_BASE/receiver.out"
  receiver "receive $name $hash $size" "$payload" "$output" || { cat "$output"; die 'receiver отверг корректный payload'; }
  assert_file "/opt/xui-backups/incoming/$name" 'архив не опубликован'
  assert_file "/opt/xui-backups/incoming/$name.sha256" 'sidecar не опубликован'
  pass 'доставка receiver атомарно опубликована'

  for injected in 'bash -i' 'cat /etc/shadow' 'receive "bad" 1 2' 'receive //x 00 1'; do
    if receiver "$injected" x "$output"; then
      die "receiver принял инъекцию: $injected"
    else
      rc=$?
    fi
    ((rc != 0)) || die "receiver принял инъекцию: $injected"
    assert_contains "$output" 'ERROR INVALID_COMMAND' "receiver не сообщил INVALID_COMMAND: $injected"
  done
  pass 'SSH_ORIGINAL_COMMAND проходит строгую allowlist-проверку'

  flock /opt/xui-backups/.store.lock -c 'sleep 5' & lock_pid=$!
  sleep .2
  if receiver "receive xui-backup-20260917T120001Z-99999-111111112.tar.gz.gpg $hash $size" "$payload" "$output"; then
    die 'receiver принял данные при удерживаемой .store.lock'
  else
    rc=$?
  fi
  kill "$lock_pid" 2>/dev/null || true; wait "$lock_pid" 2>/dev/null || true
  ((rc != 0)) || die 'receiver принял данные при удерживаемой .store.lock'
  assert_contains "$output" 'ERROR RECEIVER_BUSY' 'receiver не вернул RECEIVER_BUSY'
  pass 'receiver корректно отклоняет конкурентную запись'
}

scenario_health_and_retention() {
  step 'health and retention: corruption, orphan quarantine, expiry and lock serialization'
  local health_out retention_out old_invalid lock_pid rc i archive
  health_out="$SANDBOX_BASE/health.out"; retention_out="$SANDBOX_BASE/retention.out"
  /opt/xui-backups/bin/xui-backup-health.sh >"$health_out" || { cat "$health_out"; die 'health отклонил проверенный свежий архив'; }
  assert_contains "$health_out" 'STATUS=OK' 'health не вернул STATUS=OK'
  printf x >>/opt/xui-backups/incoming/*.tar.gz.gpg
  if /opt/xui-backups/bin/xui-backup-health.sh >"$health_out"; then
    die 'health не сообщил порчу архива'
  else
    rc=$?
  fi
  ((rc == 2)) || die "health должен вернуть 2 при порче, rc=$rc"
  rm -f -- /opt/xui-backups/incoming/*

  cat >/etc/x-ui/sync.env <<'EOF'
MAX_AGE_DAYS=14
KEEP_INVALID_DAYS=7
EOF
  for i in {1..6}; do
    archive="xui-backup-2026080${i}T000000Z-99999-00000000${i}.tar.gz.gpg"
    printf '%s' "$i" >/opt/xui-backups/incoming/$archive
    sha256sum /opt/xui-backups/incoming/$archive | sed "s#  /opt/xui-backups/incoming/#  #" >/opt/xui-backups/incoming/$archive.sha256
    touch -d '30 days ago' /opt/xui-backups/incoming/$archive /opt/xui-backups/incoming/$archive.sha256
  done
  touch /opt/xui-backups/incoming/xui-backup-20260701T000000Z-99999-999999999.tar.gz.gpg
  printf stale >/opt/xui-backups/invalid/expired.corrupt; touch -d '10 days ago' /opt/xui-backups/invalid/expired.corrupt
  chown -R xbackup:xbackup /opt/xui-backups/incoming /opt/xui-backups/invalid
  su -s /bin/bash xbackup -c "PATH='$PATH' /opt/xui-backups/bin/xui-backup-retention.sh" >"$retention_out" || { cat "$retention_out"; die 'retention завершился ошибкой'; }
  [[ ! -e /opt/xui-backups/invalid/expired.corrupt ]] || die 'retention не удалил invalid старше 7 дней'
  [[ "$(find /opt/xui-backups/incoming -name '*.tar.gz.gpg' | wc -l)" == 3 ]] || die 'retention не сохранил ровно KEEP_MIN_ARCHIVES=3'
  find /opt/xui-backups/invalid -name '*999999999*.orphaned' -print -quit | grep -q . || die 'orphan не перемещён в invalid'
  pass 'retention соблюдает quarantine TTL и минимум валидных архивов'

  flock /opt/xui-backups/.store.lock -c 'sleep 5' & lock_pid=$!; sleep .2
  su -s /bin/bash xbackup -c "PATH='$PATH' /opt/xui-backups/bin/xui-backup-retention.sh" >"$retention_out" 2>&1 & local retention_pid=$!
  sleep .5
  kill -0 "$retention_pid" 2>/dev/null || die 'retention не ожидает установленную lock (проверьте контракт блокировки)'
  kill "$retention_pid" "$lock_pid" 2>/dev/null || true; wait "$retention_pid" 2>/dev/null || true; wait "$lock_pid" 2>/dev/null || true
  pass 'retention сериализуется на .store.lock без изменения данных'
}

scenario_failover() {
  step 'failover: fail-fast config, promote and standby'
  local out="$SANDBOX_BASE/failover.out" rc
  cat >/etc/x-ui/sync.env <<'EOF'
PRIMARY_IP=192.0.2.1
SEND_TELEGRAM=0
EOF
  chmod 0600 /etc/x-ui/sync.env
  if /opt/xui-backups/bin/xui-failover.sh promote >"$out" 2>&1; then
    die 'failover принял пустой TRANSIT_PORT_MAP'
  else
    rc=$?
  fi
  ((rc != 0)) || die 'failover принял пустой TRANSIT_PORT_MAP'
  assert_contains "$out" 'TRANSIT_PORT_MAP is required' 'failover не дал понятную ошибку конфигурации'

  cat >/etc/x-ui/sync.env <<'EOF'
PRIMARY_IP=192.0.2.1
TRANSIT_PORT_MAP="2096:39285 53810:39284"
STANDBY_MODE_FILE=/etc/x-ui/standby-mode
SEND_TELEGRAM=0
EOF
  chmod 0600 /etc/x-ui/sync.env
  printf 'STANDBY\n' >/etc/x-ui/standby-mode
  /opt/xui-backups/bin/xui-failover.sh promote >"$out" 2>&1 || { cat "$out"; die 'promote завершился ошибкой'; }
  assert_contains /etc/x-ui/standby-mode PROMOTED 'режим не стал PROMOTED'
  assert_contains "$MOCK_LOG" 'stop xui-standby-sync.timer' 'sync timer не остановлен'
  assert_contains "$MOCK_LOG" 'enable --now xui-backup.timer' 'backup timer не включён'
  [[ ! -s "$IPTABLES_STATE" ]] || die 'DNAT chain не очищена при promote'
  /opt/xui-backups/bin/xui-failover.sh standby --yes >"$out" 2>&1 || { cat "$out"; die 'standby завершился ошибкой'; }
  assert_contains /etc/x-ui/standby-mode STANDBY 'режим не вернулся в STANDBY'
  [[ "$(grep -c '^-A' "$IPTABLES_STATE")" == 2 ]] || die 'не восстановлены две DNAT rules'
  pass 'failover fail-fast и FSM-переходы проверены в изоляции'
}

scenario_primary() {
  step 'primary: actual dry-run and restore integrity validation without database replacement'
  local out="$SANDBOX_BASE/primary.out" db_before archive payload work hash rc
  sqlite3 /etc/x-ui/x-ui.db 'CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO users(name) VALUES("current");'
  chmod 0600 /etc/x-ui/x-ui.db
  cat >/etc/x-ui/.env <<'EOF'
PRIMARY_LOCAL_RECIPIENT=0123456789012345678901234567890123456789
SECONDARY_SYNC_RECIPIENT=abcdefabcdefabcdefabcdefabcdefabcdefabcd
SEND_TELEGRAM=0
EXPORT_JSON=0
EOF
  chmod 0600 /etc/x-ui/.env
  "$ROOT_DIR/primary-node/xui-backup.sh" --dry-run >"$out" 2>&1 || { cat "$out"; die 'xui-backup --dry-run завершился ошибкой'; }
  assert_contains "$out" '[dry-run] validation ok' 'dry-run не подтвердил validation/manifest pipeline'
  pass 'xui-backup --dry-run проверил DB/config и симуляцию ротации'

  work="$SANDBOX_BASE/restore-payload"; mkdir -p "$work"
  sqlite3 "$work/x-ui.db" 'CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO users(name) VALUES("backup");'
  python3 - "$work" <<'PY'
import hashlib, json, pathlib, sys
p=pathlib.Path(sys.argv[1]); db=p/'x-ui.db'
h=hashlib.sha256(db.read_bytes()).hexdigest()
(p/'manifest.json').write_text(json.dumps({'format':'xui-backup-manifest-v2','database_sha256':h,'tables':['users'],'json_export':False}), encoding='utf-8')
PY
  tar -C "$work" -czf "$SANDBOX_BASE/payload.tar.gz" manifest.json x-ui.db
  archive=/backup/x-ui/xui-backup-20260917T120000Z-99999-333333333.tar.gz.gpg
  cp -- "$SANDBOX_BASE/payload.tar.gz" "$archive"
  hash="$(sha256sum "$archive" | awk '{print $1}')"; printf '%s  %s\n' "$hash" "${archive##*/}" >"$archive.sha256"
  db_before="$(sha256sum /etc/x-ui/x-ui.db | awk '{print $1}')"
  if printf '\n' | "$ROOT_DIR/primary-node/xui-restore.sh" "$archive" >"$out" 2>&1; then
    die 'restore применил изменения без подтверждения'
  else
    rc=$?
  fi
  ((rc == 3)) || { cat "$out"; die "restore validation без подтверждения должен отмениться rc=3, rc=$rc"; }
  assert_contains "$out" 'Все проверки backup успешно пройдены.' 'restore не завершил проверку целостности до отмены'
  [[ "$db_before" == "$(sha256sum /etc/x-ui/x-ui.db | awk '{print $1}')" ]] || die 'restore изменил DB в validation-only сценарии'
  pass 'restore проверил SHA/GPG/manifest/SQLite и не применил изменения без RESTORE'
}

main() {
  setup_mocks
  setup_filesystems
  scenario_receiver_and_security
  scenario_health_and_retention
  scenario_failover
  scenario_primary
  printf '\n%sВСЕ BASH ИНТЕГРАЦИОННЫЕ ПРОВЕРКИ УСПЕШНО ПРОЙДЕНЫ%s\n' "$C_GREEN" "$C_RESET"
}
main "$@"
