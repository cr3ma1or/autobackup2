# 3x-ui Replication & Standby Suite

Комплект для резервного копирования, защищённой доставки и контролируемой репликации данных **3x-ui** между двумя Linux-узлами. Проект предназначен для схемы **Primary + Hot Standby**: активный Primary обслуживает пользователей, а Secondary принимает зашифрованные архивы, поддерживает локальную копию клиентских данных и при необходимости вручную переводится в активную роль.

> [!WARNING]
> Это инфраструктурный проект для 3x-ui и SQLite. Перед эксплуатацией в production проверьте конфигурацию на тестовых узлах, сохраните отдельные ключи GPG/SSH и подготовьте процедуру failover. Автоматического решения о переключении нет: `promote` всегда должен выполнять оператор после подтверждения недоступности Primary.

## Что решает проект

- создаёт согласованные SQLite-снимки 3x-ui без прямого копирования WAL-базы;
- упаковывает и шифрует архив для двух GPG-получателей, добавляет SHA-256 sidecar и при необходимости передаёт его по SSH;
- принимает архивы на Secondary через ограниченный SSH forced command от непривилегированного пользователя `xbackup`;
- проверяет свежесть, размер, SHA-256, GPG-подпись, содержимое tar-архива, SQLite и схему до любых изменений;
- избирательно синхронизирует клиентов, traffic-данные и разрешённые настройки в одной явной SQLite-транзакции;
- синхронно поддерживает данные и в таблице `clients`, и в JSON-массиве `inbounds.settings.clients`;
- сохраняет локальную идентичность Secondary: веб- и subscription-порты, TLS-пути, Telegram-настройки и Reality-конфигурацию Inbound 1;
- создаёт rollback-снимок перед применением изменений и восстанавливает БД при ошибке;
- предоставляет ручной failover с защитой от split-brain и L4 DNAT-транзитом на Primary в режиме ожидания.

## Схема работы

```mermaid
flowchart LR
    P[Primary\n3x-ui + xui-backup] -->|GPG-архив + SHA-256\nSSH forced command| R[Secondary\nxbackup receiver]
    R --> A[/opt/xui-backups/incoming]
    A --> S[xui-standby sync]
    S --> D[Secondary 3x-ui SQLite]
    E[Клиентский DNS] --> G[Secondary gateway]
    G -->|STANDBY: DNAT| P
    G -->|PROMOTED: локальный 3x-ui| D
```

В `STANDBY` Secondary транзитирует клиентский трафик на Primary и может принимать репликацию. В `PROMOTED` DNAT отключён, Secondary обслуживает трафик локально, а синхронизация входящих архивов блокируется. Это исключает неконтролируемое слияние данных во время аварии.

## Компоненты

| Расположение | Компонент | Назначение |
|---|---|---|
| `primary-node/xui-backup.sh` | `xui-backup` | Создание, проверка, шифрование и доставка backup. |
| `primary-node/xui-restore.sh` | `xui-restore` | Проверенное интерактивное восстановление SQLite. |
| `secondary-node/xui-backup-receiver.sh` | SSH receiver | Атомарный приём архива с проверкой протокола. |
| `secondary-node/xui-backup-retention.sh` | retention | Карантин повреждённых архивов и очистка хранилища. |
| `secondary-node/xui-backup-health.sh` | health probe | Read-only проверка свежести и SHA-256. |
| `secondary-node/xui_standby_sync/` | `xui-standby` | Планирование, транзакционное применение и rollback. |
| `secondary-node/xui-failover.sh` | `xui-failover` | Ручной переход ролей и управление DNAT/timers. |
| `install.sh` | installer | Установка компонентов, конфигурационных шаблонов и systemd units. |

## Требования

- GNU/Linux с `systemd`, Bash 4.4+ и Python 3.10+;
- доступ `root` для установки и рабочих операций;
- `sqlite3`, GnuPG, OpenSSH, `tar`, `gzip`, `flock`, `shred`, `iptables`;
- сетевой доступ **Primary → Secondary SSH**;
- отдельные GPG-ключи и отдельный SSH-ключ доставки.

Production-код Python использует только стандартную библиотеку. Установщик поддерживает `apt-get`, `dnf` и `yum` для установки системных зависимостей.

## Быстрый старт

1. На обоих серверах клонируйте одинаковую ревизию репозитория.
2. Установите роли:

   ```bash
   # На активном узле
   sudo ./install.sh --role primary

   # На резервном узле
   sudo ./install.sh --role secondary
   ```

3. Настройте ключи и конфигурацию:
   - импортируйте публичный ключ Secondary в keyring Primary и укажите `SECONDARY_SYNC_RECIPIENT` в `/etc/x-ui/.env`;
   - импортируйте публичный ключ Primary в `/etc/x-ui/standby/secondary-sync-gnupg` и укажите его полный fingerprint в `TRUSTED_GPG_SIGNER_FINGERPRINTS` файла `/etc/x-ui/sync.env`;
   - добавьте выделенный публичный SSH-ключ Primary в `/home/xbackup/.ssh/authorized_keys` Secondary с ограничениями из `secondary-node/examples/authorized_keys.example`;
   - заполните `PRIMARY_IP`, `STANDBY_IP` и `TRANSIT_PORT_MAP` на Secondary.
4. До включения таймеров выполните безопасные проверки:

   ```bash
   # Primary
   sudo xui-backup --dry-run

   # Secondary
   sudo xui-standby validate --json
   sudo xui-standby sync --dry-run --json
   ```

5. Включите расписания:

   ```bash
   # Primary
   sudo systemctl enable --now xui-backup.timer

   # Secondary
   sudo systemctl enable --now xui-standby-sync.timer xui-backup-retention.timer
   ```

Подробная последовательность, требования к ключам и unattended-режим приведены в [INSTALL.md](INSTALL.md).

## Операции

### Контроль синхронизации

```bash
sudo xui-standby status
sudo xui-standby plan --json
sudo xui-standby check-backup --backup /opt/xui-backups/incoming/<archive>.tar.gz.gpg --json
sudo xui-standby sync --dry-run --json
sudo xui-standby sync --json
```

Для отката используйте только проверенный снимок и явное подтверждение:

```bash
sudo xui-standby rollback --run-id <UTC_TIMESTAMP-UUID> --yes
```

### Аварийное переключение

Сначала изолируйте Primary от боевого трафика и подтвердите, что он не сможет обслуживать записи. Затем на Secondary:

```bash
sudo xui-failover status
sudo xui-failover promote
```

`promote` останавливает синхронизацию, очищает DNAT и включает локальный `xui-backup.timer`; поэтому конфигурация DR-backup и секреты GPG на Secondary должны быть подготовлены **до** аварии. Возврат в резерв выполняется только после failback данных на Primary:

```bash
sudo xui-failover standby
```

Полный регламент, включая защиту от split-brain и failback, находится в [docs/failover.md](docs/failover.md).

## Безопасность и границы проекта

- Не храните GPG secret keys, приватные SSH-ключи, Telegram token или реальные IP-адреса в Git.
- Секретные файлы должны принадлежать `root:root` и иметь режим `0600`; каталоги ключей и снимков — `0700`.
- Не заменяйте SQLite-файл командой `cp` при активном WAL: используйте `xui-backup` или `.backup` API.
- Не запускайте одновременно `xui-failover`, `xui-standby sync` и ручные операции с БД.
- Secondary не является автоматическим consensus/failover-решением: оператор отвечает за изоляцию Primary до `promote`.

## Документация

- [Установка и первичная настройка](INSTALL.md)
- [Архитектура](docs/architecture.md)
- [Развёртывание: пути, конфигурация и systemd](docs/deployment.md)
- [Правила репликации и инварианты](docs/business-rules.md)
- [Модель данных и allowlist](docs/database.md)
- [Регламент failover/failback](docs/failover.md)
- [Мониторинг](docs/monitoring.md)
- [Матрица возможностей](docs/features.md)
- [Заметки о релизе](RELEASE_NOTES.md)

## Проверка перед публикацией или релизом

Из корня репозитория:

```bash
./test_bash_suite.sh
cd secondary-node/xui_standby_sync
sudo "$(command -v uv)" run --with pytest pytest
uv run --with ruff ruff check .
uv run --with mypy mypy .
```

Bash-набор использует изолированное окружение и требует passwordless `sudo` либо запуска от `root`. Python-тесты также используют root-owned временные каталоги.

## Лицензия

Проект распространяется по лицензии [MIT](LICENSE).
