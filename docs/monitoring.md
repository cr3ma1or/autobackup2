# Monitoring & Observability Specification

## 1. Контуры наблюдения

1. **Data Plane:** Мониторинг сокетов (443, 2096, 60291, 39284), счетчиков IPTables DNAT, проверка исходящего шлюза wireproxy.
2. **Control Plane:** Состояние systemd-юнитов и таймеров, статус режима `/etc/x-ui/standby-mode`.
3. **Storage Plane:** Сенсор `xui-backup-health.sh`, проверка возраста последнего архива (<26 ч), целостность SHA-256.
4. **Database Plane:** `PRAGMA integrity_check;`, контроль размера WAL-файлов (<10 МБ).

## 2. Контракт зонда SLA (`xui-backup-health.sh`)

- Вывод в STDOUT строго в формате Key-Value:
  `STATUS=<OK|CRITICAL> version=<VER> archive=<NAME> bytes=<INT> age_hours=<INT> checksum=<OK|FAIL> reason=<CODE> LAST_RECEIVER_LOG=<LOG>`
- Коды возврата:
  - `0` — Состояние OK.
  - `1` — Синтаксическая ошибка CLI.
  - `2` — Критическое нарушение SLA / сбой целостности.

### Реестр диагностических кодов (`reason=`)

- `health_check_passed` (OK) — Контур исправен.
- `no_archives_found` (CRITICAL) — Хранилище входящих бэкапов пусто.
- `stale_backup` (CRITICAL) — Возраст бэкапа превышает 26 часов.
- `checksum_fail` (CRITICAL) — Несовпадение фактического SHA-256 с файлом `.sha256`.
- `missing_sidecar_hash` (CRITICAL) — Архив обнаружен без sidecar-файла контрольной суммы.
- `storage_unreachable` (CRITICAL) — Отсутствуют права чтения на каталог incoming.

## 3. Эталонная матрица сетевых слушателей

### Primary Node

- `443/tcp` — Inbound VLESS Reality
- `39284/tcp` — Веб-панель управления 3x-ui
- `39285/tcp` — Выдача подписок клиентам
- `40000/tcp` — Локальный исходящий WARP SOCKS5
- `<PRIMARY_SSH_PORT>/tcp` — Системный порт SSH

### Secondary Node

- `443/tcp` — Inbound VLESS Reality (локальный резерв / транзит DNAT)
- `2096/tcp` — Локальный саб-сервер (в режиме ожидания перехвачен DNAT)
- `60291/tcp` — Локальная веб-панель управления 3x-ui
- `53810/tcp` — Транзитный порт доступа к веб-панели Primary Node
- `<SECONDARY_SSH_PORT>/tcp` — Системный SSH и порт приемника бэкапов
