# Database Architecture & Data Replication Specification

## 1. Параметры СУБД и режим доступа

- **Файл базы данных:** `/etc/x-ui/x-ui.db` (права `0600 root:root`).
- **Режим WAL:** `PRAGMA journal_mode=WAL;`, `PRAGMA synchronous=NORMAL;`.
- **Файлы транзакций:** `x-ui.db`, `x-ui.db-wal`, `x-ui.db-shm`.
- **Требование к снятию снапшотов:** Физическое копирование файла базы на Secondary выполняется **строго после вызова `systemctl stop x-ui.service`**, гарантирующего сброс страниц WAL в основной файл.

## 2. Структура ключевых таблиц (DDL)

### 2.1. Таблица `clients`

```sql
CREATE TABLE clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id INTEGER NOT NULL REFERENCES inbounds(id) ON DELETE CASCADE,
    email TEXT,
    sub_id TEXT,
    uuid TEXT NOT NULL,
    password TEXT,
    auth TEXT,
    flow TEXT,
    security TEXT,
    reverse TEXT,
    limit_ip INTEGER DEFAULT 0,
    total_gb INTEGER DEFAULT 0,
    expiry_time INTEGER DEFAULT 0,
    enable INTEGER DEFAULT 1,
    tg_id INTEGER DEFAULT 0,
    group_name TEXT,
    comment TEXT,
    reset INTEGER DEFAULT 0,
    created_at INTEGER,
    updated_at INTEGER,
    wg_private_key TEXT,
    wg_public_key TEXT,
    wg_allowed_ips TEXT,
    wg_pre_shared_key TEXT,
    wg_keep_alive INTEGER DEFAULT 0,
    secret TEXT,
    ad_tag TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_uuid ON clients(uuid);
```

2.2. Таблица `inbounds`

```
CREATE TABLE inbounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    up INTEGER DEFAULT 0,
    down INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    remark TEXT,
    enable INTEGER DEFAULT 1,
    expiry_time INTEGER DEFAULT 0,
    traffic_reset TEXT,
    last_traffic_reset_time INTEGER DEFAULT 0,
    listen TEXT,
    port INTEGER NOT NULL,
    protocol TEXT NOT NULL,
    settings TEXT,           -- JSON со списком клиентов {"clients": [...]}
    stream_settings TEXT,    -- JSON с Reality-ключами и SNI
    tag TEXT UNIQUE,
    sniffing TEXT,
    node_id INTEGER,
    sub_sort_index INTEGER DEFAULT 0,
    traffic_reset_day INTEGER DEFAULT 0,
    share_addr_strategy TEXT,
    share_addr TEXT,
    origin_node_guid TEXT
);
```

2.3. Таблица `client_traffics`

```
CREATE TABLE client_traffics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    inbound_id INTEGER NOT NULL REFERENCES inbounds(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    up INTEGER DEFAULT 0,
    down INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    expiry_time INTEGER DEFAULT 0,
    enable INTEGER DEFAULT 1
);
```

## 3. Архитектура Dual-Layer Storage

Dual-Layer merge реализован и подтверждён E2E smoke-тестом. При каждой мутации клиентов данные изменяются в рамках **одной** SQLite-транзакции одновременно:

1. В строках таблицы `clients`.
2. В сериализованном JSON-массиве `inbounds.settings.clients` соответствующего `inbound_id`.

Запрещено выполнять вставку, обновление или удаление в `clients` без синхронной модификации JSON ядра Xray. До `COMMIT` план и инварианты проверяются; при ошибке выполняется `ROLLBACK`.

## 4. Спецификация `allowlist.json`

Файл `/etc/x-ui/standby/allowlist.json` регулирует границы репликации:

JSON

```
{
  "clients": {
    "matching_key": "uuid",
    "fallback_matching_key": "email",
    "allowed_columns": [
      "inbound_id",
      "uuid",
      "email",
      "sub_id",
      "password",
      "auth",
      "flow",
      "security",
      "reverse",
      "limit_ip",
      "total_gb",
      "expiry_time",
      "enable",
      "tg_id",
      "group_name",
      "comment",
      "reset",
      "updated_at",
      "wg_private_key",
      "wg_public_key",
      "wg_allowed_ips",
      "wg_pre_shared_key",
      "wg_keep_alive",
      "secret",
      "ad_tag"
    ]
  },
  "inbounds": {
    "preserve_inbound_ids": [1],
    "sync_additional_inbounds": true
  },
  "settings": {
    "allowed_keys": [
      "subEnableRouting",
      "subRoutingRules"
    ]
  },
  "assert_invariants": [
    "webPort",
    "subPort",
    "tgBotEnable",
    "subURI"
  ]
}
```

Фактический файл использует верхний уровень `tables` для описаний `inbounds`, `clients`, `client_traffics` и `settings`; ниже приведён соответствующий рабочий формат:

```json
{
  "tables": {
    "clients": {
      "matching_key": "uuid",
      "fallback_matching_key": "email",
      "allowed_columns": ["inbound_id", "uuid", "email"]
    },
    "settings": {
      "matching_key": "key",
      "allowed_keys": ["subEnableRouting", "subRoutingRules"]
    }
  },
  "assert_invariants": ["webPort", "subPort", "tgBotEnable", "subURI"]
}
```

Полный поддерживаемый шаблон находится в `secondary-node/examples/allowlist.json.example`.

## 5. Защита Inbound 1 Reality

Ядро репликации сохраняет локальные данные `stream_settings` Inbound 1: Reality network identity, ключи, SNI/server names и external proxy. Эти параметры не могут быть перезаписаны donor-базой. После слияния инварианты Standby проверяются до фиксации транзакции.

## 6. Протокол транзакционности в Python

Соединение с БД на запись открывается с ручным управлением границами транзакций:

```
conn = sqlite3.connect(target_db_path, isolation_level=None)
conn.execute("PRAGMA foreign_keys = ON;")
conn.execute("PRAGMA busy_timeout = 5000;")

try:
    conn.execute("BEGIN IMMEDIATE;")
    # Dual-Layer mutations and invariant assertions...
    conn.execute("COMMIT;")
except Exception:
    conn.execute("ROLLBACK;")
    raise
```

`isolation_level=None` исключает неявные транзакции; границы `BEGIN IMMEDIATE`, `COMMIT` и `ROLLBACK` остаются явными и контролируемыми.
```
