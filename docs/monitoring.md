# Monitoring & Observability Specification

## 1. Контуры наблюдения

1. **Data Plane:** Мониторинг сокетов (443, 2096, 60291, <example>39284</example>), счетчиков IPTables DNAT, проверка исходящего шлюза wireproxy.
2. **Control Plane:** Состояние systemd-юнитов и таймеров, статус режима `/etc/x-ui/standby-mode`.
3. **Storage Plane:** Сенсор `xui-backup-health.sh`, проверка возраста последнего архива (<26 ч), целостность SHA-256.
4. **Database Plane:** `PRAGMA integrity_check;`, контроль размера WAL-файлов (<10 МБ).

## 2. Контракт зонда SLA (`xui-backup-health.sh`)

- Вывод в STDOUT имеет Key-Value формат. Первая строка: `STATUS=<OK|WARN|CRITICAL> version=<VER> ...`; вторая строка: `LAST_RECEIVER_LOG=<LOG>`.
  - Успех: `STATUS=OK version=<VER> archive=<NAME> bytes=<INT> age_hours=<INT> checksum=OK` — поля `reason=` нет.
  - Устаревший, но целый архив: `STATUS=WARN ... archive=<NAME> reason=stale age_hours=<INT> age_seconds=<INT> max_age_hours=26 checksum=OK`.
  - Остальные ошибки имеют `STATUS=CRITICAL ... reason=<CODE>`.
- Коды возврата:
  - `0` — Состояние OK.
  - `1` — синтаксическая ошибка CLI либо WARN (устаревший архив).
  - `2` — Критическое нарушение SLA / сбой целостности.

### Реестр диагностических кодов (`reason=`)

- `missing_dependency`, `bash_too_old`, `incoming_directory_missing_or_unreadable`, `no_archives_found` (CRITICAL) — ошибка окружения либо хранилище входящих бэкапов пусто.
- `unparseable_timestamp`, `invalid_timestamp_epoch`, `archive_or_sidecar_missing_or_empty`, `checksum_fail`, `archive_vanished_during_check`, `invalid_archive_size`, `unexpected_error`, `interrupted` (CRITICAL) — фактические диагностические коды скрипта.
- `stale` (WARN) — возраст бэкапа превышает 26 часов; это не `CRITICAL` и не `stale_backup`.
- Успешный статус не выдаёт `reason=health_check_passed`.

## 3. Эталонная матрица сетевых слушателей

### Primary Node

- `443/tcp` — Inbound VLESS Reality
- `<example>39284</example>/tcp` — Веб-панель управления 3x-ui
- `<example>39285</example>/tcp` — Выдача подписок клиентам
- `40000/tcp` — Локальный исходящий WARP SOCKS5
- `<PRIMARY_SSH_PORT>/tcp` — Системный порт SSH

### Secondary Node

- `443/tcp` — Inbound VLESS Reality (локальный резерв / транзит DNAT)
- `2096/tcp` — Локальный саб-сервер (в режиме ожидания перехвачен DNAT)
- `60291/tcp` — Локальная веб-панель управления 3x-ui
- `<example>53810</example>/tcp` — Транзитный порт доступа к веб-панели Primary Node
- `<SECONDARY_SSH_PORT>/tcp` — Системный SSH и порт приемника бэкапов
