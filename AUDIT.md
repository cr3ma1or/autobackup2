# Production-Ready Audit — 3x-ui Replication & Standby Suite

**Дата аудита:** 2026-09-17  
**Объект:** рабочий код репозитория (Bash, `install.sh`, production-модули Python, unit-файлы, примеры и документация); каталоги `tests/` не входили в статический scope.  
**Метод:** read-only reverse engineering, проверка конфигурационных контрактов, статический анализ кода и запуск предусмотренных проверок. Факты о текущем хосте (реальные `iptables`, systemd, порты, владельцы и схема БД) не предполагались и не запрашивались.

---

## 1. Production Readiness Verdict

# НЕ ГОТОВ К ПРОДУ

Архитектурная основа сильная: роли изолированы, receiver ограничен forced command, архивы защищены dual-recipient GPG, синхронизация использует preflight, advisory locks, SQLite `BEGIN IMMEDIATE`, снапшоты и dual-layer модель клиентов. Однако четыре дефекта уровня **HIGH** создают недопустимые риски при первом развёртывании/переключении и при подтверждении целостности данных. Дополнительно тестовый прогон в фактическом окружении завершился с **177 ошибками** (из-за отсутствия доступа к `/root`, см. раздел 7), поэтому заявление `RELEASE_NOTES.md` о deployment readiness не подтверждено этим запуском.

### Обязательные до промышленного ввода

1. Исправить lifecycle promoted Standby: не включать backup timer без подготовленного DR-конфига и keyring.
2. Исключить TOFU при pinning SSH host key в installer.
3. Останавливать уже запущенный `xui-standby-sync.service` до promotion и согласовать единый lock protocol.
4. Гарантировать откат при post-commit invariant/integrity failure и не маскировать первичную SQLite-ошибку ошибкой `ROLLBACK`.
5. Разрешить конфликт root-only `sync.env` и запуск retention от `xbackup`.
6. Актуализировать конфигурационную/операционную документацию и повторить тесты root в Linux/WSL2.

### Желательные

- Закрепить `AES256`, если это именно требование продукта, а не описание предпочтения GnuPG.
- Pinning trusted signer добавить и в standalone `xui-restore`.
- Валидировать диапазон портов до создания/подключения iptables chain; проверить `net.ipv4.ip_forward`.
- Ввести проверку свободного места до backup, snapshot и receive; обеспечить alert при невозможности retention.
- Усилить systemd sandboxing (`NoNewPrivileges=yes`, capability restrictions, `ProtectKernel*`, `PrivateTmp`) после проверки нужных прав.

### Необязательные

- Единый машиночитаемый конфигурационный schema/validator для Bash, Python и installer.
- Интеграционные тесты с реальным systemd/iptables в disposable VM.
- Атомарная публикация unit-файлов и явная проверка symlink для installer output.

---

## 2. Реконструкция архитектуры

```mermaid
flowchart LR
  P[Primary root: xui-backup.timer] -->|SQLite .backup; tar/gzip; GPG sign+encrypt| A[/backup/x-ui archive]
  A -->|SSH dedicated key + known_hosts| R[Secondary xbackup: forced receiver]
  R --> I[/opt/xui-backups/incoming]
  I -->|systemd timer, root| S[xui-standby sync]
  S -->|STANDBY marker + locks + signature + plan| D[/etc/x-ui/x-ui.db]
  F[root: xui-failover] -->|STANDBY / PROMOTED| S
  F -->|DNAT chain| P
  F -->|promotion| X[Local x-ui]
```

### Поток и доверительные границы

- **Primary/root:** `xui-backup.sh` снимает согласованный SQLite backup, формирует JSON/manifest, шифрует и подписывает архив для local и Secondary recipient. `xui-restore.sh` — аварийное локальное восстановление.
- **Передача:** SSH использует выделенный private key и pinned `known_hosts`; протокол передаёт filename/hash/size через `SSH_ORIGINAL_COMMAND`.
- **Secondary/xbackup:** receiver принимает только узкий протокол, лимитирует размер, сверяет hash, публикует архив и sidecar в incoming. Этот пользователь не должен читать `/etc/x-ui/sync.env` или менять target DB.
- **Secondary/root:** `xui-standby` читает root-only конфигурацию, принимает архив только в `STANDBY`, берёт sync/store locks, проверяет GPG signature, строит read-only plan, останавливает `x-ui`, создаёт rollback snapshot, применяет SQL transaction и запускает service.
- **Failover/root:** управляет `/etc/x-ui/standby-mode`, `/run/xui-standby.lock`, systemd timers/services и iptables DNAT. FSM: `STANDBY → PROMOTED → (ручная реконсилиация) → STANDBY`. Технически `RECONCILING` описан в `docs/business-rules.md`, но самостоятельного автоматизированного state transition в рассмотренном коде не обнаружено.

### Положительные факты

- Все рассмотренные Bash-скрипты проходят `bash -n`; используют shebang, `set -Eeuo pipefail`, `umask 077`.
- SQLite executor использует `sqlite3.connect(..., isolation_level=None)`, foreign keys, busy timeout и `BEGIN IMMEDIATE`.
- Planner открывает DB read-only, сравнивает mapping inbound/client, mass-deletion guard и защищает Inbound 1.
- Engine поддерживает relational `clients` и `inbounds.settings.clients` в одной транзакции.
- Archive extractor запрещает ссылки/необычные tar members, ограничивает DB size и запускается в отдельном процессе с timeout.
- Файлы конфигурации, marker, DB, allowlist, archive и lock защищаются проверками ownership/mode/symlink в Python.

---

## 3. Реестр дефектов и рисков

| ID | Компонент / файл | Критичность | Описание |
|---|---|---:|---|
| A-01 | `install.sh`, `xui-failover.sh` | HIGH | После promotion включается `xui-backup.timer`, но secondary installation не provision-ит требуемые backup `.env` и primary backup GPG home. DR backup не работает. |
| A-02 | `install.sh` | HIGH | `ssh-keyscan` без out-of-band fingerprint verification pin-ит ключ атакующего и включает transfer: TOFU/MITM при bootstrap. |
| A-03 | `secondary-node/xui-failover.sh` | HIGH | Promotion останавливает timer, но не уже запущенный sync service; DB может изменяться во время смены роли. |
| A-04 | `workflow.py` | HIGH | После commit ошибка integrity/invariant намеренно не откатывается; Standby service запускается с DB, не прошедшей post-commit verification. |
| A-05 | `xui-backup-retention.sh`, retention unit, `install.sh` | MEDIUM | Retention запускается как `xbackup`, но читает `/etc/x-ui/sync.env` с `0600 root:root`; `MAX_AGE_DAYS` игнорируется. |
| A-06 | `install.sh` | MEDIUM | `TRANSIT_PORT_MAP` проверяется лишь regex до iptables, допускает 0 и >65535 и оставляет частично подключённую NAT chain при ошибке. |
| A-07 | `engine.py` | MEDIUM | При ошибке до успешного `BEGIN IMMEDIATE` unconditional `ROLLBACK` может заменить исходную SQLite ошибку на `cannot rollback - no transaction is active`. |
| A-08 | `primary-node/xui-restore.sh` | MEDIUM | Standalone restore дешифрует, но не pin-ит expected signer fingerprint и не требует проверяемую trusted signature. |
| A-09 | `primary-node/xui-backup.sh` | MEDIUM | Заявленный AES-256 не закреплён параметром GPG; фактический cipher выбирается по preferences recipients. |
| A-10 | `xui-backup-health.sh` | LOW | Общая INT/TERM trap завершается с health CRITICAL code 2, а не POSIX 130/143, вопреки `docs/patterns.md`. |
| A-11 | `install.sh` | LOW | `write_unit()` перезаписывает destination без явной anti-symlink проверки/атомарной публикации. |
| A-12 | `install.sh` | LOW | DNAT setup не проверяет `net.ipv4.ip_forward`; правила могут существовать без маршрутизации трафика. |
| A-13 | Documentation | MEDIUM | Команды, частоты таймеров, protocol и конфигурационные переменные противоречат фактическому коду/шаблонам. |
| A-14 | Verification | MEDIUM | Предписанный pytest не прошёл: 41 passed, 177 errors; среда не root и tests принудительно используют `/root`. |

---

## 4. Детальный разбор и точечные исправления

### A-01 — неполный DR lifecycle после promotion (HIGH)

**Локализация:** `install.sh:742–747, 856–910`; `secondary-node/xui-failover.sh:424–433`.

Secondary installer копирует `xui-backup`/`xui-restore` и создаёт secondary sync keyring (`secondary-sync-gnupg`), но не создаёт обязательные для `xui-backup` `/etc/x-ui/.env`, `primary-local-gnupg`, `PRIMARY_LOCAL_RECIPIENT`, `SECONDARY_SYNC_RECIPIENT`. Promotion включает backup timer. Первый timer execution завершается validation failure — на активном DR узле не появляются новые backup.

**Минимальное безопасное исправление:** до отдельного явного provisioning workflow не включать timer автоматически.

```diff
--- a/secondary-node/xui-failover.sh
+++ b/secondary-node/xui-failover.sh
@@
-  systemctl enable --now "$BACKUP_TIMER"
+  systemctl disable --now "$BACKUP_TIMER"
@@
-  log INFO "node promoted successfully"
+  log INFO "node promoted; backup timer remains disabled until DR backup configuration is provisioned"
```

Полное исправление: отдельный secondary-active backup env/keyring, импорт и верификация DR recipient keys, preflight до `enable --now`, а также точная инструкция failback direction.

### A-02 — MITM в первом SSH pinning (HIGH)

**Локализация:** `install.sh:586–602`.

`ssh-keyscan -H "$secondary_host"` не аутентифицирует ключ. Installer сразу сохраняет output и включает transfer. `StrictHostKeyChecking=yes` защищает только последующие соединения с уже потенциально атакующим ключом.

**Исправление:** вывести fingerprint, требовать ручное подтверждение через независимый канал и только затем записывать verified public key. Безопасный минимальный вариант — оставить `TRANSFER_ENABLED=0`:

```diff
-          printf '%s\n' "$keyscan_output" >>"$PRIMARY_KNOWN_HOSTS"
-          set_env_value "$PRIMARY_TRANSFER_ENV_FILE" TRANSFER_ENABLED 1
+          printf '%s\n' "$keyscan_output" | ssh-keygen -lf -
+          warn "Verify this fingerprint out of band; transfer remains disabled."
```

### A-03 — sync race при promotion (HIGH)

**Локализация:** `secondary-node/xui-failover.sh:424–433`; связанный workflow `workflow.py:92–247`.

Остановка `.timer` не прерывает oneshot `.service`, уже стартованную timer. Скрипт берёт `/run/xui-standby-sync.lock`, Python берёт тот же lock, но promotion не делает `systemctl stop xui-standby-sync.service`; lock proof не заменяет остановку уже исполняемого lifecycle.

```diff
   systemctl stop "$SYNC_TIMER"
+  systemctl stop xui-standby-sync.service
   flush_transit_chain
```

После правки проверить timeout/stop semantics unit и добавить интеграционный тест «sync running + promote». Документ `docs/failover.md` сейчас возлагает остановку sync на оператора, что не является достаточной автоматической гарантией.

### A-04 — post-commit failure оставляет непроверенную БД (HIGH)

**Локализация:** `secondary-node/xui_standby_sync/workflow.py:177–217`.

`mutation_committed=True` устанавливается перед `verify_integrity()` и `verify_invariants()`. При их ошибке ветка `elif mutation_committed` только логирует, что rollback intentionally suppressed. Затем finally запускает `x-ui`; результат sync failure не восстанавливает предыдущее verified состояние. Это противоречит BR-503: при failure post-merge assertions требуется rollback.

```diff
-                    if (
-                        mutation_started
-                        and not mutation_committed
-                        and snapshot is not None
-                    ):
+                    if mutation_started and snapshot is not None:
                         rollback_attempted = True
                         try:
                             restore_rollback_snapshot(
@@
-                    elif mutation_committed:
-                        logger.critical(
-                            "Post-commit synchronization failure; database rollback "
-                            "is intentionally suppressed",
-                            exc_info=True,
-                        )
```

Нужен отдельный тест post-commit invariant failure, подтверждающий: snapshot restored, x-ui starts only после successful restore; при restore failure — CRITICAL alert и явная non-zero failure.

### A-05 — root-only config недоступен retention (MEDIUM)

**Локализация:** `secondary-node/xui-backup-retention.sh:49, 94–113`; `secondary-node/examples/xui-backup-retention.service`; installer создаёт `sync.env` `0600 root:root`.

Service работает `User=xbackup`, поэтому `[[ -r /etc/x-ui/sync.env ]]` false и retention всегда использует default 14 дней. Шаблон `sync.env.example` не содержит `MAX_AGE_DAYS`.

**Исправление:** не делать секретный `sync.env` world-readable. Вынести несекретный policy в `/etc/x-ui/backup-retention.env` (`root:root:0644`) и читать только его:

```diff
-readonly CONFIG_FILE="/etc/x-ui/sync.env"
+readonly CONFIG_FILE="/etc/x-ui/backup-retention.env"
```

Installer обязан создать файл с `MAX_AGE_DAYS`; template/documentation должны описать owner/mode и default.

### A-06 — неатомарная NAT setup при bad port (MEDIUM)

**Локализация:** `install.sh:434–472`.

Regex `^[0-9]{1,5}:[0-9]{1,5}$` пропускает `0:443` и `99999:443`. Цепочка/jump создаются до успешного добавления всех rules. Ошибка iptables прерывает script с `set -e`, оставляя ранее добавленные rules.

```diff
       if [[ ! "$pair" =~ ^([0-9]{1,5}):([0-9]{1,5})$ ]]; then
@@
       fi
+      external_port="${pair%%:*}"; internal_port="${pair##*:}"
+      if ((10#$external_port < 1 || 10#$external_port > 65535 ||
+          10#$internal_port < 1 || 10#$internal_port > 65535)); then
+        warn "Out-of-range TRANSIT_PORT_MAP entry '$pair'"
+        mappings=(); break
+      fi
```

Валидация должна завершиться **до** изменения iptables; при runtime failure необходим rollback созданной chain/jump.

### A-07 — маскирование SQLite error (MEDIUM)

**Локализация:** `secondary-node/xui_standby_sync/engine.py:72–251`.

Если `BEGIN IMMEDIATE` не стартует transaction, global `except BaseException` вызывает `ROLLBACK`, который сам может бросить `sqlite3.OperationalError`. Диагностика lock/DB failure будет потеряна.

```diff
-    except BaseException:
-        connection.execute("ROLLBACK;")
+    except BaseException:
+        if connection.in_transaction:
+            connection.execute("ROLLBACK;")
         raise
```

Также удалить вводящий в заблуждение комментарий о default `isolation_level`: здесь он явно `None` и explicit `BEGIN` обязателен.

### A-08 — restore без trusted signer pinning (MEDIUM)

**Локализация:** `primary-node/xui-restore.sh:845–847`.

Restore использует `gpg --decrypt`, затем проверяет manifest/hash/integrity, но не проверяет `GOODSIG`/`VALIDSIG` на expected primary fingerprint. Внутренний hash не аутентифицирует источник: его может сформировать автор вредоносного, но расшифровываемого архива.

**Исправление:** получить trusted full fingerprint через безопасный config, использовать `--status-fd`, требовать `GOODSIG` и exact `VALIDSIG <fingerprint>` до extraction/restore. Нельзя оставлять placeholder fingerprint в production.

### A-09 — AES-256 описан, но не enforced (MEDIUM)

**Локализация:** `primary-node/xui-backup.sh:1318–1320`; `docs/deployment.md`.

Фактическая GPG command не задаёт cipher. Если AES-256 — обязательное свойство, зафиксировать:

```diff
   gpg --batch --yes --trust-model always \
+    --cipher-algo AES256 \
     -r "$PRIMARY_LOCAL_RECIPIENT" -r "$SECONDARY_SYNC_RECIPIENT" \
```

Если cipher agility является намеренным дизайном, вместо правки нужно удалить категоричное «AES-256» из документации и описать negotiated OpenPGP cipher.

### A-10…A-12 — низкие, но подтверждённые hardening gaps

- `xui-backup-health.sh:85`: заменить combined trap на handlers `exit 130` (INT) и `exit 143` (TERM).
- `install.sh:232–238`: output unit писать в same-directory temp with `O_EXCL`/safe ownership, `fsync` и `mv --`; запретить symlink destination.
- `install.sh:460–485`: перед NAT activation проверить `sysctl -n net.ipv4.ip_forward` равно `1`; иначе не активировать transit и дать remediation.

---

## 5. Матрица конфигурационных контрактов

| Контур | Ключи, подтверждённо потребляемые кодом | Состояние |
|---|---|---|
| Primary `/etc/x-ui/.env` | `PRIMARY_LOCAL_RECIPIENT`, `SECONDARY_SYNC_RECIPIENT`, `SEND_TELEGRAM`, `TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_PROXY_URL`, `EXPORT_JSON` | Шаблон в целом соответствует. |
| Primary transfer env | `TRANSFER_ENABLED`, `TRANSFER_HOST`, `TRANSFER_USER`, `TRANSFER_PORT`, `TRANSFER_KEY`, `TRANSFER_KNOWN_HOSTS` | Шаблон соответствует; deployment doc дополнительно показывает `TRANSFER_TIMEOUT_SEC`, который необходимо сверить/документировать по фактической реализации. |
| Secondary `/etc/x-ui/sync.env` | Telegram; `PRIMARY_IP`, `STANDBY_IP`, `CUSTOM_RESERVED_PORTS`; `TRUSTED_GPG_SIGNER_FINGERPRINTS`, `REQUIRE_GPG_SIGNATURE`; `CMD_TIMEOUT`, `MAX_AGE_SECONDS`, `MAX_CLOCK_SKEW_SECONDS`, `MAX_UNPACK_SIZE_BYTES`, `MAX_ROLLBACK_COPIES`; paths | Python allowlist `_ALLOWED_ENV_KEYS` не включает `TRANSIT_PORT_MAP`: его использует failover Bash. Шаблон содержит его корректно. |
| Retention policy | `MAX_AGE_DAYS` в retention | Отсутствует в `sync.env.example` и недоступен service user: A-05. |
| CLI override | `--timeout → CMD_TIMEOUT`, `--allowlist`; `--unsafe-backup-path` | Реализовано, но docs path override предупреждение следует ограничить: arbitrary production override требует security review ownership/ancestor chain. |

**Нулевое hardcode:** production scripts не содержат конкретных IP из grep-проверки. Примеры `10.0.0.1/10.0.0.2` обнаружены в `secondary-node/xui_standby_sync/README.md` (и build artifact `.venv`), то есть это documentation example, не runtime default. Документ `docs/deployment.md` содержит конкретные operational port identities; это заявленная topology, а не скрытый code default.

---

## 6. Аудит документации

| Файл | Неточность / пробел | Требуемое изменение |
|---|---|---|
| `INSTALL.md` | Говорит, что Secondary backup timer включается при promotion без prerequisite DR backup config. | После A-01: либо описать provisioning и preflight, либо указать, что timer намеренно не включается автоматически. |
| `INSTALL.md` | Описывает sync «каждые 12 часов 04:00/16:00 UTC». | Unit содержит `00/2:00:00`, то есть каждые 2 часа; привести текст к unit либо изменить unit сознательно. |
| `docs/failover.md` | Promotion описан как включающий local backup timer; требует от оператора вручную дождаться/остановить sync. | Отразить автоматическую остановку active service, availability prerequisites backup и точный recovery behaviour. |
| `docs/business-rules.md` | BR-301 описывает иной receiver protocol (`xui-backup-put`, другой порядок аргументов), чем код receiver (`receive <name> <sha256> <size>`). | Документировать exact grammar из фактического receiver или унифицировать implementation и документ. |
| `docs/business-rules.md` | BR-503 требует rollback при любом post-merge assertion failure, но workflow сейчас intentionally suppresses post-commit rollback. | После A-04 либо привести код к правилу, либо явно изменить правило (не рекомендуется). |
| `docs/deployment.md` | Показывает `/opt/xui-backups/bin/xui-backup-retention.sh`, но unit запускает `/usr/local/bin/xui-backup-retention`; CLI example `rollback --latest --yes` отсутствует в parser. | Исправить paths и заменить команду на `rollback --run-id <id> --yes` или `--snapshot <path> --yes`. |
| `docs/deployment.md` | Показывает `MAX_AGE_DAYS`, `MAX_SIZE_GB`, `KEEP_MIN_ARCHIVES` в primary `.env`; transfer timeout; они должны соответствовать реальному consumer/template. | Создать таблицу «ключ / consumer / default / owner / mode» и удалить неподдерживаемые ключи. |
| `docs/patterns.md` | Требование signal code нарушается health probe. | После A-10 код должен соответствовать документу; оставить как normative standard. |
| `RELEASE_NOTES.md` | «ready for deployment» сделано до чистого root test run и при нерешённых lifecycle/config gaps. | Изменить на conditional readiness после закрытия A-01…A-07 и успешного root CI/provisioning test. |

---

## 7. Выполненная верификация

| Проверка | Результат | Интерпретация |
|---|---|---|
| `bash -n install.sh primary-node/*.sh secondary-node/*.sh` | PASS | Синтаксис Bash валиден. |
| `uv run --with ruff ruff check .` | PASS (`ruff=0`) | Style/static lint без findings. |
| `uv run --with mypy mypy .` | PASS (`mypy=0`) | Типовая проверка проходит. |
| `uv run --with pytest pytest` | FAIL: **41 passed, 177 errors** | Тест fixture создаёт temp directories в `/root`; фактический process не имеет прав. Это environmental failure, а не доказательство functional failure. Тем не менее release readiness в данной среде не подтверждён. |
| `systemd-analyze verify` examples | Команды не существуют по final paths в source checkout | Ожидаемо для templates вне установленного хоста; dependency/ExecStart syntax должен быть проверен после install в disposable VM. |

Для полноценного acceptance выполнить предписанные команды от UID 0 в Linux/WSL2, затем E2E: Primary backup → real SSH forced receiver → standby dry-run/sync → active sync + promotion race → forced post-commit invariant failure → rollback → failback.

---

## 8. Итоговая дорожная карта

1. **P0:** закрыть A-01…A-07, обновить unit/templates/docs синхронно.
2. **P1:** закрыть A-08/A-09, добавить explicit disk-space/retention alerts и firewall transaction rollback.
3. **P2:** внедрить machine-readable config schema и VM-based destructive integration suite.
4. Принять production release только после successful root test run, clean install/uninstall/reinstall, независимой проверки SSH/GPG fingerprints и tabletop failover/failback exercise с зафиксированными результатами.
