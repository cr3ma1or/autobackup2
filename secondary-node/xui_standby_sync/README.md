## xui-standby-sync

**Transactional standby synchronization engine for 3x-ui**

Модульный Python-пакет для безопасной синхронизации базы данных 3x-ui между primary и standby узлами с гарантией целостности и возможностью отката при ошибках.

---

## 📋 Краткое описание

`xui-standby-sync` выполняет selective database synchronization:

- ✅ Проверяет режим standby перед любой операцией
- ✅ Валидирует целостность, происхождение и свежесть backup
- ✅ Безопасно расшифровывает и извлекает source SQLite DB
- ✅ Строит детерминированный план синхронизации (read-only)
- ✅ Атомарно применяет изменения в одной SQLite транзакции
- ✅ Откатывает изменения при любой ошибке после остановки сервиса
- ✅ Гарантирует контролируемый restart x-ui
- ✅ Выдаёт machine-readable JSON для автоматизации

---

## 🚀 Быстрый старт

### Требования

- **Python:** 3.10+
- **ОС:** GNU/Linux с systemd
- **Привилегии:** root
- **Утилиты:** `systemctl`, `gpg`, `tar`, `shred` (опционально)

### Установка

```bash
# Клонируем репозиторий
git clone <repo-url>
cd xui-standby-sync

# Устанавливаем пакет
pip install -e .

# Или с dev зависимостями для тестирования
pip install -e ".[dev]"
```

### Первый запуск

```bash
# Проверяем, что система готова
xui-standby validate --json

# Смотрим статус
xui-standby status

# Проверяем имеющийся backup
xui-standby check-backup --backup /path/to/backup.tar.gz.gpg

# Делаем сухой прогон (без изменений)
xui-standby sync --dry-run --json

# Выполняем реальную синхронизацию
xui-standby sync --json
```

---

## 📖 Команды CLI

### `xui-standby sync` - главная команда

Выполняет полный цикл синхронизации: валидация → остановка сервиса → применение плана → рестарт.

```bash
# Синхронизация с последним backup из incoming directory
xui-standby sync

# С конкретным backup файлом
xui-standby sync --backup /opt/backups/custom-backup.tar.gz.gpg

# Разрешить stale backup (старше max-age)
xui-standby sync --force

# Только проверка без изменений
xui-standby sync --dry-run

# JSON output для скриптов
xui-standby sync --json

# С подробными логами
xui-standby sync -v

# Unsafe backup path (без проверки incoming directory)
xui-standby sync --backup /tmp/backup.gpg --unsafe-backup-path

# Все опции вместе
xui-standby sync --backup /opt/backup.gpg --dry-run --force --json -v
```

### `xui-standby plan` - построение плана

Строит детерминированный план синхронизации без изменения target DB или сервиса.

```bash
# Построить план и вывести JSON
xui-standby plan --json

# С конкретным backup
xui-standby plan --backup /path/to/backup.gpg --json

# Проверить план для custom конфига
xui-standby plan --config /etc/custom-sync.env --json
```

### `xui-standby validate` - проверка окружения

Полная валидация конфигурации и окружения перед синхронизацией.

```bash
# Базовая проверка
xui-standby validate

# JSON результат
xui-standby validate --json

# С подробностями
xui-standby validate -v
```

**Проверяет:**
- Режим standby (файл `/etc/x-ui/standby-mode`)
- Failover lock отсутствие
- Права доступа критических файлов
- Конфигурацию и allowlist
- GPG home и trusted fingerprints
- systemctl доступность
- Target DB целостность и схему
- Snapshots directory безопасность

### `xui-standby check-backup` - проверка backup

Валидирует backup без расшифровки и применения.

```bash
# Проверить backup
xui-standby check-backup --backup /opt/backups/latest.gpg

# JSON результат
xui-standby check-backup --backup /opt/backups/latest.gpg --json

# Разрешить unsafe path
xui-standby check-backup --backup /tmp/backup.gpg --unsafe-backup-path --json
```

**Проверяет:**
- Путь безопасность
- Checksum sidecar (`.sha256`)
- Свежесть (max-age)
- GPG подпись и signer fingerprint
- Расшифровку
- Tar layout (один `x-ui.db`)
- Source DB целостность и схему

### `xui-standby rollback` - откат к snapshot

Восстанавливает target DB из сохранённого snapshot.

```bash
# Откат к конкретному snapshot (обязателен --yes)
xui-standby rollback --snapshot /etc/x-ui/standby-snapshots/runs/20260912T120000Z-uuid/target-before-sync.db --yes

# Откат к последнему snapshot
xui-standby rollback --latest --yes

# По run-id
xui-standby rollback --run-id 20260912T120000Z-uuid --yes

# JSON результат
xui-standby rollback --latest --yes --json
```

**Операции:**
1. Остановить x-ui
2. Удалить `-wal` и `-shm` файлы
3. Восстановить DB из snapshot
4. Проверить целостность
5. Запустить x-ui
6. Убедиться в active статусе

### `xui-standby status` - статус системы

Read-only инспекция текущего статуса.

```bash
# Текстовый результат
xui-standby status

# JSON для обработки
xui-standby status --json

# С подробностями
xui-standby status -v
```

**Включает:**
- Standby mode статус
- Failover lock наличие
- Lock файлы состояние
- Target DB целостность
- x-ui сервис статус
- Последний backup информацию
- Последний run результат

### Legacy alias

Для обратной совместимости:

```bash
# Старый формат всё ещё работает
xui-standby-sync --help
xui-standby-sync --dry-run --force --json
```

---

## ⚙️ Конфигурация

### Приоритет значений

Конфигурация применяется в следующем порядке (последний побеждает):

```
defaults → /etc/x-ui/.env → /etc/x-ui/sync.env → --config PATH → CLI args
```

### Конфигурационные файлы

#### `/etc/x-ui/.env` - базовая конфигурация

```bash
# Telegram уведомления
SEND_TELEGRAM=true
TG_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TG_CHAT_ID=-1001234567890
TG_PROXY_URL=http://proxy.example.com:8080  # опционально

# IP адреса для stream_settings замену
PRIMARY_IP=10.0.0.1
STANDBY_IP=10.0.0.2

# Зарезервированные порты (недоступны для import)
CUSTOM_RESERVED_PORTS=22,80,443

# GPG конфигурация
TRUSTED_GPG_SIGNER_FINGERPRINTS=ABCD1234ABCD1234ABCD1234ABCD1234ABCD1234
REQUIRE_GPG_SIGNATURE=true

# Таймауты и лимиты
CMD_TIMEOUT=30
MAX_AGE_SECONDS=21600         # 6 часов
MAX_CLOCK_SKEW_SECONDS=300    # 5 минут
MAX_UNPACK_SIZE_BYTES=536870912  # 500 MB
MAX_ROLLBACK_COPIES=5
```

#### `/etc/x-ui/sync.env` - sync-специфичная конфигурация

Переопределяет значения из `.env` только для sync операций.

#### `/etc/x-ui/standby/allowlist.json` - таблица допустимых колонок

```json
{
  "tables": {
    "inbounds": {
      "matching_key": "tag",
      "allowed_columns": ["id", "port", "protocol", "tag", "settings", "stream_settings", "enable", "sniffing"]
    },
    "clients": {
      "matching_key": "uuid",
      "fallback_matching_key": "email",
      "allowed_columns": ["id", "inbound_id", "uuid", "email", "enable", "total_gb", "expiry_time", "sub_id", "tg_id"]
    },
    "client_traffics": {
      "matching_key": "email",
      "allowed_columns": ["id", "inbound_id", "email", "up", "down", "total"]
    },
    "settings": {
      "allowed_keys": ["webPort", "webBasePath", "webCertFile", "webKeyFile", "xrayTemplateConfig", "tgBotEnable", "tgBotToken"]
    }
  },
  "assert_invariants": ["webPort", "webBasePath"]
}
```

---

## 🔒 Безопасность

### Обязательные проверки

1. **Standby mode validation** - проверка файла `/etc/x-ui/standby-mode`
2. **Failover lock check** - отсутствие `/run/xui-standby.lock`
3. **File permissions** - root owner, `0600`/`0700`, без symlinks
4. **GPG signature** - trusted signer fingerprint
5. **Backup integrity** - SHA-256 checksum verification
6. **Schema validation** - совместимость source и target DB

### Безопасный откат

При ошибке после остановки сервиса:

1. SQL ROLLBACK (если транзакция активна)
2. Восстановление immutable snapshot
3. Integrity check восстановленной DB
4. Запуск x-ui (только если restore успешен)
5. Verification active статуса

### Защита от injection

- Нет `shell=True` в subprocess вызовах
- Все SQL values через параметры, не f-string
- Командные аргументы как массив, не строка
- GPG fingerprints white-list валидация

---

## 📊 Примеры использования

### Сценарий 1: Плановая синхронизация

```bash
#!/bin/bash
set -e

echo "=== Валидация ==="
xui-standby validate --json | jq .

echo "=== Проверка backup ==="
xui-standby check-backup --backup /opt/xui-backups/incoming/latest.gpg --json | jq .

echo "=== Планирование ==="
xui-standby plan --backup /opt/xui-backups/incoming/latest.gpg --json | jq '.plan_summary'

echo "=== Синхронизация (dry-run) ==="
xui-standby sync --dry-run --json | jq '.success'

echo "=== Реальная синхронизация ==="
RESULT=$(xui-standby sync --json)
echo "$RESULT" | jq .

if echo "$RESULT" | jq -e '.success == true' > /dev/null; then
  echo "✅ Синхронизация успешна"
else
  echo "❌ Синхронизация не удалась"
  exit 1
fi
```

### Сценарий 2: Мониторинг и уведомления

```bash
#!/bin/bash

# Проверяем статус каждые 5 минут
while true; do
  STATUS=$(xui-standby status --json)
  
  # Проверяем целостность DB
  if echo "$STATUS" | jq -e '.target_db_integrity == "failed"' > /dev/null; then
    curl -X POST https://alerts.example.com/db-corruption \
      -H "Content-Type: application/json" \
      -d "$STATUS"
  fi
  
  # Проверяем сервис
  if echo "$STATUS" | jq -e '.xui_service_active == false' > /dev/null; then
    curl -X POST https://alerts.example.com/service-down \
      -H "Content-Type: application/json" \
      -d "$STATUS"
  fi
  
  sleep 300
done
```

### Сценарий 3: Откат при проблеме

```bash
#!/bin/bash

# Если последний sync failed, откатываем
LAST_RUN=$(xui-standby status --json | jq -r '.last_run')

if echo "$LAST_RUN" | jq -e '.success == false' > /dev/null; then
  RUN_ID=$(echo "$LAST_RUN" | jq -r '.run_id')
  
  echo "Откатываем run_id: $RUN_ID"
  xui-standby rollback --run-id "$RUN_ID" --yes --json | jq .
  
  # Отправляем уведомление
  echo "Откат выполнен для $RUN_ID" | mail -s "xui-standby rollback" admin@example.com
fi
```

---

## 🔄 systemd интеграция

### Service unit

```ini
[Unit]
Description=3x-ui standby synchronization
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=root
Group=root
ExecStart=/usr/local/bin/xui-standby sync --json
TimeoutStartSec=10min
TimeoutStopSec=5min
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/etc/x-ui /opt/xui-backups /run /var/log
StandardOutput=journal
StandardError=journal
```

### Timer unit

```ini
[Unit]
Description=Run 3x-ui standby synchronization periodically

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Persistent=true
RandomizedDelaySec=30s

[Install]
WantedBy=timers.target
```

### Использование

```bash
# Включаем автоматическую синхронизацию
sudo systemctl enable xui-standby-sync.timer
sudo systemctl start xui-standby-sync.timer

# Проверяем статус
sudo systemctl status xui-standby-sync.timer

# Просмотр логов
journalctl -u xui-standby-sync -f

# Ручной запуск
sudo systemctl start xui-standby-sync.service
```

---

## 📝 JSON Output

Все команды поддерживают `--json` для machine-readable вывода:

### Sync результат

```json
{
  "success": true,
  "run_id": "20260912T120000Z-abc123",
  "plan": {
    "backup": {
      "archive_name": "xui-backup-20260912T120000Z-test.tar.gz.gpg",
      "age_hours": 0.5,
      "checksum_valid": true,
      "signature_verified": true
    },
    "summary": {
      "inbounds_create": 1,
      "inbounds_delete": 0,
      "clients_insert": 2,
      "clients_update": 3,
      "clients_delete": 0,
      "traffic_insert": 2,
      "traffic_update": 3,
      "traffic_delete": 0
    }
  },
  "service_was_stopped": true,
  "service_restarted": true,
  "rollback_attempted": false,
  "rollback_succeeded": null,
  "error_message": null,
  "exit_code": 0
}
```

### Status результат

```json
{
  "standby_mode": "STANDBY",
  "standby_marker_secure": true,
  "failover_lock_present": false,
  "sync_lock_busy": false,
  "store_lock_busy": false,
  "target_db_integrity": "ok",
  "xui_service_active": true,
  "latest_backup": {
    "name": "xui-backup-20260912T120000Z-test.tar.gz.gpg",
    "age_hours": 0.5,
    "checksum_valid": true
  },
  "last_run": {
    "run_id": "20260912T120000Z-prev",
    "success": true,
    "rollback_succeeded": null
  }
}
```

---

## 🧪 Тестирование

### Запуск тестов

```bash
# Все тесты
pytest tests/ -v

# С coverage
pytest tests/ --cov=xui_standby_sync --cov-report=html

# Конкретный модуль
pytest tests/test_planner.py -v

# С фильтром
pytest tests/ -k "test_security" -v

# Dry-run (проверка синтаксиса)
python -m py_compile xui_standby_sync/*.py
```

### Линтинг и type check

```bash
# Ruff линтинг
ruff check src tests

# Type checking
mypy src/xui_standby_sync

# Форматирование
ruff format src tests
```

---

## 🚨 Troubleshooting

### "Synchronization is not allowed in standby mode"

**Решение:** Убедитесь что `/etc/x-ui/standby-mode` существует и содержит `STANDBY`:

```bash
cat /etc/x-ui/standby-mode
# Output: STANDBY
```

### "Backup is stale"

**Решение:** Используйте `--force` для stale backup или проверьте `MAX_AGE_SECONDS`:

```bash
xui-standby sync --force
```

### "GPG signature is missing, invalid, or signed by an untrusted fingerprint"

**Решение:** Проверьте конфигурацию GPG:

```bash
# Список trusted fingerprints
grep TRUSTED_GPG_SIGNER_FINGERPRINTS /etc/x-ui/.env

# Проверка backup подписи
xui-standby check-backup --backup /path/to/backup.gpg --json
```

### "Service did not become active"

**Решение:** Проверьте статус x-ui:

```bash
systemctl status x-ui
journalctl -u x-ui -n 50
```

### "Lock is busy"

**Решение:** Дождитесь завершения предыдущей синхронизации:

```bash
# Проверка активных процессов
xui-standby status --json | jq '.sync_lock_busy'

# Если процесс зависал, проверьте PID
lsof /run/xui-standby-sync.lock
```

---

## 📚 Документация

- **ТЗ-UPGRADE.md** - полная техническая спецификация
- **tests/** - примеры использования в тестах
- `--help` - встроенная справка по командам

---

## 📄 Лицензия

Proprietary

---

## 👥 Support

По вопросам обратитесь к DevOps Team.

---

## 📊 Версия

**xui-standby-sync v2.0.0** - Python 3.10+, GNU/Linux, systemd
