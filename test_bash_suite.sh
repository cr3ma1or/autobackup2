#!/usr/bin/env bash
# ============================================================================
# Script:       test_bash_suite.sh
# Description:  Secure isolated integration-test entrypoint.
# ============================================================================
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/.test_bash_suite_impl.sh" "$@"

# The former in-place runner is intentionally left below as unreachable history.
# The implementation above is isolated in a root-only namespace and cleans all
# temporary mounts through EXIT/INT/TERM/ERR traps.

C_RESET='\033[0m'
C_RED='\033[0;31m'
C_GREEN='\033[0;32m'
C_YELLOW='\033[0;33m'
C_BLUE='\033[0;34m'

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR"

# 0. Самоизоляция через unshare (Mount + Network Namespaces)
if [[ "${IS_ISOLATED_ENV:-0}" != "1" ]]; then
  if [[ $EUID -ne 0 ]]; then
    printf "${C_YELLOW}[INFO] Запуск требует sudo для изоляции пространств имен (unshare -m -n)...${C_RESET}\n"
    exec sudo unshare -m -n --propagation private env IS_ISOLATED_ENV=1 ROOT_DIR="$ROOT_DIR" bash "$0" "$@"
  else
    exec unshare -m -n --propagation private env IS_ISOLATED_ENV=1 ROOT_DIR="$ROOT_DIR" bash "$0" "$@"
  fi
fi

SANDBOX_BASE="$(mktemp -d /tmp/xui_bash_test.XXXXXX)"
chmod 0755 "$SANDBOX_BASE"
MOCK_BIN="$SANDBOX_BASE/mock_bin"
MOCK_LOG="$SANDBOX_BASE/systemctl.log"

cleanup() {
  local rc=$?
  trap - EXIT INT TERM ERR
  printf "\n${C_BLUE}[CLEANUP] Размонтирование и удаление изолированной песочницы...${C_RESET}\n"
  umount -l /bin/systemctl /usr/bin/systemctl 2>/dev/null || true
  umount -l /opt/xui-backups 2>/dev/null || true
  umount -l /etc/x-ui 2>/dev/null || true
  umount -l /backup/x-ui 2>/dev/null || true
  rm -rf -- "$SANDBOX_BASE"
  exit "$rc"
}
trap cleanup EXIT INT TERM

log_step() { printf "${C_BLUE}==>${C_RESET} %s\n" "$1"; }
pass() { printf "${C_GREEN}[PASS]${C_RESET} %s\n" "$1"; }
fail() { printf "${C_RED}[FAIL]${C_RESET} %s\n" "$1"; exit 1; }

# 1. Подготовка изолированных каталогов и моков
log_step "Инициализация изолированных файловых систем tmpfs..."

mkdir -p "$MOCK_BIN"
chmod 0755 "$MOCK_BIN"
cat << 'EOF' > "$MOCK_BIN/systemctl"
#!/usr/bin/env bash
echo "$@" >> "${MOCK_LOG_FILE:-/tmp/mock_systemctl.log}"
exit 0
EOF
chmod 0755 "$MOCK_BIN/systemctl"

cat << 'EOF' > "$MOCK_BIN/netfilter-persistent"
#!/usr/bin/env bash
exit 0
EOF
chmod 0755 "$MOCK_BIN/netfilter-persistent"

touch "$MOCK_LOG"
chmod 0666 "$MOCK_LOG"
export MOCK_LOG_FILE="$MOCK_LOG"
export PATH="$MOCK_BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

# Проверяем или создаем пользователя xbackup
if ! id -u xbackup >/dev/null 2>&1; then
  useradd -r -s /bin/bash -d /home/xbackup xbackup 2>/dev/null || true
fi
usermod -s /bin/bash xbackup 2>/dev/null || true
passwd -u xbackup 2>/dev/null || true

# Монтируем виртуальные изолированные каталоги поверх системных
mkdir -p /opt/xui-backups /etc/x-ui /backup/x-ui /run
mount -t tmpfs -o mode=0755 tmpfs_opt /opt/xui-backups
mount -t tmpfs -o mode=0755 tmpfs_etc /etc/x-ui
mount -t tmpfs -o mode=0700 tmpfs_bck /backup/x-ui

mkdir -p /opt/xui-backups/incoming /opt/xui-backups/invalid /opt/xui-backups/bin
chmod 0755 /opt/xui-backups /opt/xui-backups/bin
chown root:root /opt/xui-backups /opt/xui-backups/bin

chown xbackup:xbackup /opt/xui-backups/incoming /opt/xui-backups/invalid
chmod 0700 /opt/xui-backups/incoming /opt/xui-backups/invalid

touch /opt/xui-backups/.store.lock /opt/xui-backups/receiver.log
chown xbackup:xbackup /opt/xui-backups/.store.lock /opt/xui-backups/receiver.log
chmod 0600 /opt/xui-backups/.store.lock /opt/xui-backups/receiver.log

# Копируем тестируемые скрипты и выдаем корректные права
cp "$ROOT_DIR/secondary-node/xui-backup-receiver.sh" /opt/xui-backups/bin/
cp "$ROOT_DIR/secondary-node/xui-backup-retention.sh" /opt/xui-backups/bin/
cp "$ROOT_DIR/secondary-node/xui-backup-health.sh" /opt/xui-backups/bin/
cp "$ROOT_DIR/secondary-node/xui-failover.sh" /opt/xui-backups/bin/

chown root:root /opt/xui-backups/bin/*.sh
chmod 0755 /opt/xui-backups/bin/*.sh

# ==============================================================================
# СЦЕНАРИЙ 1: Приемник (xui-backup-receiver) и SLA-зонд (xui-backup-health)
# ==============================================================================
log_step "Сценарий 1: Проверка доставки архива через xui-backup-receiver и валидация health..."

TEST_ARCHIVE_NAME="xui-backup-20260917T120000Z-99999-111111111.tar.gz.gpg"
PAYLOAD_CONTENT="DUMMY_ENCRYPTED_BACKUP_PAYLOAD_FOR_TESTING_ONLY"
TEST_SHA256="$(printf '%s' "$PAYLOAD_CONTENT" | sha256sum | awk '{print $1}')"
TEST_SIZE="${#PAYLOAD_CONTENT}"

RECV_OUT="$SANDBOX_BASE/recv.out"
set +e
su -s /bin/bash xbackup -c "
  export SSH_ORIGINAL_COMMAND='receive ${TEST_ARCHIVE_NAME} ${TEST_SHA256} ${TEST_SIZE}'
  printf '%s' '${PAYLOAD_CONTENT}' | /opt/xui-backups/bin/xui-backup-receiver.sh
" > "$RECV_OUT" 2>&1
RECV_RC=$?
set -e

if [[ $RECV_RC -ne 0 ]]; then
  printf "${C_RED}[DEBUG ERROR LOG]${C_RESET}\n"
  cat "$RECV_OUT"
  [[ -f /opt/xui-backups/receiver.log ]] && cat /opt/xui-backups/receiver.log
  fail "xui-backup-receiver завершился с кодом $RECV_RC"
fi

[[ -f "/opt/xui-backups/incoming/${TEST_ARCHIVE_NAME}" ]] || fail "Архив не появился в incoming/"
[[ -f "/opt/xui-backups/incoming/${TEST_ARCHIVE_NAME}.sha256" ]] || fail "Sidecar .sha256 не создан"
pass "Архив успешно принят и зафиксирован с контрольной суммой"

# Проверяем health-зонд
HEALTH_OUT="$(/opt/xui-backups/bin/xui-backup-health.sh 2>&1)" || fail "xui-backup-health завершился с ошибкой: $HEALTH_OUT"
[[ "$HEALTH_OUT" =~ STATUS=OK ]] || fail "xui-backup-health не вернул STATUS=OK"
pass "xui-backup-health подтвердил целостность архива и SLA (STATUS=OK)"

# ==============================================================================
# СЦЕНАРИЙ 2: Fault Injection — Повреждение архива и реакция SLA
# ==============================================================================
log_step "Сценарий 2: Fault Injection — искажение 1 байта и проверка алерта CRITICAL..."

echo "CORRUPTION_BYTE" >> "/opt/xui-backups/incoming/${TEST_ARCHIVE_NAME}"

set +e
/opt/xui-backups/bin/xui-backup-health.sh >/dev/null 2>&1
HEALTH_RC=$?
set -e

[[ $HEALTH_RC -eq 2 ]] || fail "xui-backup-health не вернул exit code 2 на битый архив (rc=$HEALTH_RC)"
pass "xui-backup-health вернул exit code 2 (CRITICAL) при нарушении контрольной суммы"

# ==============================================================================
# СЦЕНАРИЙ 3: Fault Injection — Отклонение битого потока в receiver
# ==============================================================================
log_step "Сценарий 3: Fault Injection — передача потока с неверным хэшем в receiver..."

BAD_NAME="xui-backup-20260917T120500Z-99999-222222222.tar.gz.gpg"
set +e
su -s /bin/bash xbackup -c "
  export SSH_ORIGINAL_COMMAND='receive ${BAD_NAME} 0000000000000000000000000000000000000000000000000000000000000000 10'
  printf '0123456789' | /opt/xui-backups/bin/xui-backup-receiver.sh
" >/dev/null 2>&1
RC_BAD=$?
set -e

[[ $RC_BAD -ne 0 ]] || fail "Receiver пропустил поток с поддельным хэшем"
QUARANTINED="$(find /opt/xui-backups/invalid -name "${BAD_NAME}*" | head -n 1)"
[[ -n "$QUARANTINED" ]] || fail "Поврежденный файл не был изолирован в invalid/"
pass "Поврежденный поток отклонен и отправлен в карантин invalid/"

rm -rf /opt/xui-backups/incoming/*

# ==============================================================================
# СЦЕНАРИЙ 4: Ротация и защита минимума (xui-backup-retention)
# ==============================================================================
log_step "Сценарий 4: Тестирование двухпроходной ротации и сохранения KEEP_MIN_ARCHIVES=3..."

# Генерируем 6 валидных тестовых архивов с возрастом 30 дней
for i in {1..6}; do
  ARC="xui-backup-2026080${i}T000000Z-99999-00000000${i}.tar.gz.gpg"
  (
    cd /opt/xui-backups/incoming
    touch -d "30 days ago" "$ARC"
    sha256sum "$ARC" > "${ARC}.sha256"
    touch -d "30 days ago" "${ARC}.sha256"
    chmod 0600 "$ARC"*
    chown xbackup:xbackup "$ARC"*
  )
done

# Добавляем 1 сиротский архив без .sha256 (должен уйти в invalid/)
touch "/opt/xui-backups/incoming/xui-backup-20260701T000000Z-99999-999999999.tar.gz.gpg"
chmod 0600 "/opt/xui-backups/incoming/xui-backup-20260701T000000Z-99999-999999999.tar.gz.gpg"
chown xbackup:xbackup "/opt/xui-backups/incoming/xui-backup-20260701T000000Z-99999-999999999.tar.gz.gpg"

cat << 'EOF' > /etc/x-ui/sync.env
MAX_AGE_DAYS=14
KEEP_VALID_DAYS=14
KEEP_MIN_ARCHIVES=3
KEEP_INVALID_DAYS=7
EOF
chmod 0644 /etc/x-ui/sync.env

RET_OUT="$SANDBOX_BASE/retention.out"
set +e
su -s /bin/bash xbackup -c "/opt/xui-backups/bin/xui-backup-retention.sh" > "$RET_OUT" 2>&1
RET_RC=$?
set -e

if [[ $RET_RC -ne 0 ]]; then
  printf "${C_RED}[DEBUG ERROR LOG]${C_RESET}\n"
  cat "$RET_OUT"
  [[ -f /opt/xui-backups/receiver.log ]] && cat /opt/xui-backups/receiver.log
  fail "xui-backup-retention завершился с кодом $RET_RC"
fi

ORPHAN_IN_INVALID="$(find /opt/xui-backups/invalid -name "*999999999*" | head -n 1)"
[[ -n "$ORPHAN_IN_INVALID" ]] || fail "Сиротский архив без .sha256 не был перенесен в карантин"

REMAINING_COUNT="$(find /opt/xui-backups/incoming -maxdepth 1 -name '*.tar.gz.gpg' | wc -l)"
[[ "$REMAINING_COUNT" -eq 3 ]] || fail "Ротация должна была оставить ровно 3 архива (осталось $REMAINING_COUNT)"
pass "Ротация успешно изолировала сироту и сохранила гарантированный минимум (ровно 3 архива)"

# ==============================================================================
# СЦЕНАРИЙ 5: Диспетчер аварийного переключения (xui-failover)
# ==============================================================================
log_step "Сценарий 5: Тестирование переключения FSM, цепочек iptables и таймеров..."

cat << 'EOF' > /etc/x-ui/sync.env
PRIMARY_IP="192.0.2.1"
STANDBY_IP="192.0.2.2"
TRANSIT_PORT_MAP="2096:39285 53810:39284"
STANDBY_MODE_FILE="/etc/x-ui/standby-mode"
SEND_TELEGRAM=0
EOF
chmod 0600 /etc/x-ui/sync.env
chown root:root /etc/x-ui/sync.env
printf "STANDBY\n" > /etc/x-ui/standby-mode
chmod 0644 /etc/x-ui/standby-mode

# 1. Предварительно создаем цепочку в изолированном пространстве iptables
iptables -t nat -N XUI_TRANSIT_DNAT 2>/dev/null || true
iptables -t nat -F XUI_TRANSIT_DNAT 2>/dev/null || true
iptables -t nat -A XUI_TRANSIT_DNAT -p tcp --dport 2096 -j DNAT --to-destination 192.0.2.1:39285

FAIL_OUT="$SANDBOX_BASE/failover.out"

# 2. Проверяем статус
set +e
/opt/xui-backups/bin/xui-failover.sh status > "$FAIL_OUT" 2>&1
FO_RC=$?
set -e
if [[ $FO_RC -ne 0 ]]; then
  printf "${C_RED}[DEBUG ERROR LOG: status]${C_RESET}\n"
  cat "$FAIL_OUT"
  fail "xui-failover status завершился с кодом $FO_RC"
fi

# 3. Выполняем PROMOTE
set +e
/opt/xui-backups/bin/xui-failover.sh promote --yes > "$FAIL_OUT" 2>&1
FO_RC=$?
set -e
if [[ $FO_RC -ne 0 ]]; then
  printf "${C_RED}[DEBUG ERROR LOG: promote]${C_RESET}\n"
  cat "$FAIL_OUT"
  fail "xui-failover promote завершился с кодом $FO_RC"
fi

[[ "$(< /etc/x-ui/standby-mode)" =~ "PROMOTED" ]] || fail "Маркер режима не переключился в PROMOTED"
grep -q "stop xui-standby-sync.timer" "$MOCK_LOG" || fail "Не была вызвана остановка xui-standby-sync.timer"
grep -q "enable --now xui-backup.timer" "$MOCK_LOG" || fail "Не был активирован локальный xui-backup.timer"

RULES_COUNT="$(iptables -t nat -S XUI_TRANSIT_DNAT | grep -c '^-A' || true)"
[[ "$RULES_COUNT" -eq 0 ]] || fail "Цепочка XUI_TRANSIT_DNAT не была очищена при promote (правил: $RULES_COUNT)"
pass "xui-failover promote корректно переключил режим, фаервол и таймеры"

# 4. Выполняем возврат в STANDBY
set +e
/opt/xui-backups/bin/xui-failover.sh standby --yes > "$FAIL_OUT" 2>&1
FO_RC=$?
set -e
if [[ $FO_RC -ne 0 ]]; then
  printf "${C_RED}[DEBUG ERROR LOG: standby]${C_RESET}\n"
  cat "$FAIL_OUT"
  fail "xui-failover standby завершился с кодом $FO_RC"
fi

[[ "$(< /etc/x-ui/standby-mode)" =~ "STANDBY" ]] || fail "Маркер режима не вернулся в STANDBY"
grep -q "enable --now xui-standby-sync.timer" "$MOCK_LOG" || fail "Не был включен xui-standby-sync.timer"

RESTORED_RULES="$(iptables -t nat -S XUI_TRANSIT_DNAT | grep -c '^-A' || true)"
[[ "$RESTORED_RULES" -eq 2 ]] || fail "Цепочка XUI_TRANSIT_DNAT не содержит 2 восстановленных правила (найдено: $RESTORED_RULES)"
pass "xui-failover standby успешно восстановил транзитные правила L4 и таймер репликации"

# ==============================================================================
# СЦЕНАРИЙ 6: Проверка CLI интерфейсов утилит (xui-backup / xui-restore)
# ==============================================================================
log_step "Сценарий 6: Проверка CLI интерфейсов xui-backup и xui-restore..."

"$ROOT_DIR/primary-node/xui-backup.sh" --help >/dev/null 2>&1 || fail "xui-backup.sh --help завершился с ошибкой"
"$ROOT_DIR/primary-node/xui-restore.sh" --help >/dev/null 2>&1 || fail "xui-restore.sh --help завершился с ошибкой"
pass "Утилиты xui-backup и xui-restore корректно обрабатывают аргументы CLI"

printf "\n===================================================================\n"
printf "${C_GREEN}ВСЕ ИНТЕГРАЦИОННЫЕ ТЕСТЫ BASH-СВЯЗКИ УСПЕШНО ПРОЙДЕНЫ!${C_RESET}\n"
printf "1. xui-backup-receiver: Атомарный прием, SHA-256 sidecar       [OK]\n"
printf "2. xui-backup-health:   SLA мониторинг, детект битых копий     [OK]\n"
printf "3. xui-backup-retention: Двухпроходная очистка и лимит (min 3) [OK]\n"
printf "4. xui-failover:         FSM маркер, таймеры и iptables DNAT   [OK]\n"
printf "5. xui-backup / restore: Валидация CLI аргументов              [OK]\n"
printf "===================================================================\n"