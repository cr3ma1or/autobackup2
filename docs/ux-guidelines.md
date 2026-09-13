# Operational Interface & UX Guidelines

## 1. Принципы проектирования CLI

- Обязательная поддержка флагов: `-h/--help`, `-v/--version`, `--dry-run`, `--unattended` (или `-y`).
- TTY Fail-Safe: Если операция требует подтверждения, но запущена без терминала и без `--unattended`, скрипт завершается с кодом `1`:
  `ERROR: Interactive prompt required, but no TTY is attached. Aborting for safety.`

## 2. Подтверждение опасных действий (Tokens)

Для деструктивных операций подтверждение запрашивается вводом токена в верхнем регистре:

- Восстановление базы (`xui-restore`): ввести `RESTORE`.
- Несовпадение схемы (`xui-restore`): ввести `YES`.
- Перевод в Standby (`xui-failover`): ввести `STANDBY`.

## 3. Стандарты форматирования журналов

Формат записи системного лога:
`YYYY-MM-DD HH:MM:SS UTC [LEVEL] [COMPONENT] message; key1=value1; key2=value2`

Примеры:

- `2026-09-05 03:27:31 UTC [INFO] [xui-backup] backup_completed; archive=backup-20260905.tar.gz.gpg; size=38244; sha256=a1b2...`
- `2026-09-05 03:30:15 UTC [ERROR] [xui-standby-sync] invariant_violation; field=webPort; expected=60291; actual=39284; rollback=executed`

## 4. Оповещения в Telegram (Mobile UX)

- Форматирование: Простой читаемый Markdown без горизонтального скролла.
- Градация: `#INFO`, `#WARN`, `#CRITICAL`, `#PROMOTED`.

Пример аварийного уведомления:

```text
🚨 #CRITICAL: Standby Sync FAILED & ROLLED BACK
Host: Secondary Node
Time: 2026-09-05 04:00:18 UTC
Reason: NOT NULL constraint failed: clients.inbound_id
Rollback: SUCCESS (Restored to last-good-pre-sync.db)
x-ui.service: ACTIVE (Running)
```
