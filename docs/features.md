# Features & Functional Matrix

## Модуль 1: Резервное копирование (Primary Node)

- **F-101 Consistent SQLite Dump `[DONE]`:** Горячий снимок БД через SQLite `.backup` API без деградации WAL.
- **F-102 Deterministic Tar & JSON `[DONE]`:** Детерминированная упаковка (`tar --mtime=@0 --sort=name`) и нормализованный JSON-дамп.
- **F-103 Dual-Recipient GPG `[DONE]`:** Асимметричное шифрование по 40-значным hex-отпечаткам для двух получателей.
- **F-104 Sidecar Checksum `[DONE]`:** Атомарная публикация контрольной суммы `*.sha256` до переименования основного архива.
- **F-105 Two-Level Retention `[DONE]`:** Ротация по возрасту и размеру с безусловным барьером `KEEP_MIN_ARCHIVES=3`.
- **F-106 Non-Blocking Transport `[DONE]`:** Изолированная отправка в Telegram и потоковая доставка по SSH.

## Модуль 2: Хранилище нулевого доверия (Secondary Node)

- **F-201 SSH Forced Command Receiver `[DONE]`:** Прием архивов под непривилегированным пользователем `xbackup` с regex-валидацией параметров.
- **F-202 Quarantine Engine `[DONE]`:** Изоляция поврежденных файлов в каталог `invalid/` и обработка идемпотентных повторов.
- **F-203 2-Pass Retention `[DONE]`:** Санация хранилища с проверкой контрольных сумм и удаление карантина старше 7 дней.
- **F-204 Read-Only Health SLA Probe `[DONE]`:** Сенсор состояния бэкапов с однострочным Key-Value выводом.

### Модуль 3: Транзакционная синхронизация (Standby Engine)

- **F-301 Planner & Engine Separation `[DONE]`:** Фаза планирования в режиме `read-only` и изолированная фаза применения.
- **F-302 Dual-Layer Merge `[DONE]`:** Атомарное обновление таблицы `clients` и JSON-массива в `inbounds.settings`.
- **F-303 Host Invariants Preservation `[DONE]`:** Защита сетевых портов, SSL-путей и настроек бота Secondary Node.
- **F-304 SQLite Transaction Safety `[DONE]`:** Явное управление транзакциями через `isolation_level=None`.
- **F-305 Safe Rollback Lifecycle `[DONE]`:** Создание снимка `target-before-sync.db` строго после остановки сервиса и гарантированный откат.
- **F-306 Split-Brain Guard `[DONE]`:** Запрет синхронизации, если маркер режима не равен точному значению `STANDBY`, либо существует failover-блокировка.
- **F-307 Client Pruning & Traffic Purge `[DONE]`:** Удаление отсутствующих пользователей и очистка устаревшей статистики.

**- F-308 Secure Artifact Wipe `[DONE]`:** Уничтожение временных баз данных системной утилитой `shred`.

### Модуль 4: Управление отказами (Failover & Recovery)

- **F-401 Failover Manager CLI (`xui-failover`) `[DONE]`:** Ручное переключение Secondary в боевой режим (`promote`) и возврат в режим ожидания (`standby`) с блокировками, DNAT и timer-управлением.
- **F-402 Interactive Recovery CLI (`xui-restore`) `[DONE]`:** Интерактивное восстановление базы данных на Primary со сверкой схем.
