# Установка 3x-ui Replication & Standby Suite

## Назначение и схема

Комплект создаёт зашифрованные резервные копии БД 3x-ui на **Primary** и доставляет их на **Standby**. На резервном узле `xui-standby` безопасно планирует и применяет selective merge данных, сохраняя локальную сетевую идентичность.

```mermaid
flowchart LR
    P[Primary: xui-backup] -->|GPG encrypted archive over SSH| R[xbackup receiver]
    R --> I[/opt/xui-backups/incoming]
    I --> S[Standby: xui-standby sync]
    S --> D[/etc/x-ui/x-ui.db]
```

Primary остаётся активным источником. Standby принимает архивы, хранит снапшоты и применяет синхронизацию только при маркере `STANDBY`.

## Требования

- GNU/Linux с `systemd`, Bash 4.4+ и Python 3.10+.
- Запуск установщика от `root`.
- На Primary: `sqlite3`, GnuPG, `tar`, `gzip`, OpenSSH client и `curl` при Telegram-уведомлениях.
- На Standby: Python `venv`, `sqlite3`, GnuPG, OpenSSH server и те же базовые GNU-утилиты.
- Сеть между узлами: SSH Primary → Standby; DNS/файрвол должны быть настроены отдельно.

`install.sh` устанавливает отсутствующие системные зависимости через `apt-get`, `dnf` или `yum`. Python-модуль репликации использует только стандартную библиотеку Python.

## Подготовка ключей

1. На каждом узле создайте или импортируйте отдельный GPG-ключ.
2. Экспортируйте публичный ключ Secondary и импортируйте его на Primary; экспортируйте публичный ключ Primary и импортируйте его на Standby.
3. На Primary укажите в `/etc/x-ui/.env` полные 40-символьные fingerprints `PRIMARY_LOCAL_RECIPIENT` и `SECONDARY_SYNC_RECIPIENT`.
4. На Standby внесите fingerprint подписанта Primary в `TRUSTED_GPG_SIGNER_FINGERPRINTS` файла `/etc/x-ui/sync.env`.
5. Создайте отдельный SSH-ключ доставки и добавьте его в `/home/xbackup/.ssh/authorized_keys` Standby с forced command из `secondary-node/examples/authorized_keys.example`.

Секретные env-файлы и ключи должны иметь режим `0600` и владельца `root:root`.

## Установка Primary

В корне репозитория выполните:

```bash
sudo ./install.sh --role primary
```

Установщик размещает `xui-backup`, `xui-restore`, timer и шаблон `/etc/x-ui/backup-transfer.env`. Заполните:

- `/etc/x-ui/.env`: GPG recipients, Telegram и `EXPORT_JSON`;
- `/etc/x-ui/backup-transfer.env`: `TRANSFER_ENABLED`, `TRANSFER_HOST`, `TRANSFER_USER`, `TRANSFER_PORT`, `TRANSFER_KEY`, `TRANSFER_KNOWN_HOSTS`.

Проверьте настройки безопасным запуском `sudo xui-backup --dry-run`, затем включите таймер:

```bash
sudo systemctl enable --now xui-backup.timer
systemctl list-timers xui-backup.timer
```

На Primary резервное копирование запускается ежедневно по расписанию systemd (03:20 UTC с небольшим случайным смещением).

## Установка Standby

На резервном узле из корня репозитория:

```bash
sudo ./install.sh --role secondary
```

Установщик создаёт пользователя `xbackup`, receiver, engine `xui-standby`, утилиту `xui-failover`, файлы systemd, `/etc/x-ui/sync.env`, allowlist и файл `/etc/x-ui/standby-mode` со значением `STANDBY`.

Основные настройки `/etc/x-ui/sync.env`:

- GPG и политики: `TRUSTED_GPG_SIGNER_FINGERPRINTS`, `REQUIRE_GPG_SIGNATURE`, `MAX_AGE_SECONDS`, `MAX_UNPACK_SIZE_BYTES`;
- уведомления: `SEND_TELEGRAM`, `TG_BOT_TOKEN`, `TG_CHAT_ID`, `TG_PROXY_URL`;
- сеть: `PRIMARY_IP`, `STANDBY_IP`, `CUSTOM_RESERVED_PORTS`;
- опциональные пути: `TARGET_DB_PATH`, `INCOMING_DIR`, `WORK_DIR`, `SNAPSHOTS_DIR`, `GNUPG_DIR`, `STANDBY_MODE_FILE`, `ALLOWLIST_PATH`.

Пути необязательны: при их отсутствии используются значения из `constants.py`. Заданные значения нормализуются через `Path.resolve()` и могут указывать на изолированную песочницу для тестов.

Проверьте конфигурацию и состояние:

```bash
sudo xui-standby validate --json
sudo xui-standby status
sudo xui-standby sync --dry-run --json
```

После успешной проверки включите автоматическую синхронизацию и retention:

```bash
sudo systemctl enable --now xui-standby-sync.timer xui-backup-retention.timer
systemctl list-timers xui-standby-sync.timer xui-backup-retention.timer
```

Синхронизация выполняется каждые 12 часов — в `04:00` и `16:00 UTC` — со случайным смещением до 15 минут. На Secondary также устанавливается `xui-backup.timer`, но он намеренно оставляется остановленным и отключённым в режиме `STANDBY`; это спящий таймер, который включается только после `sudo xui-failover promote`.

Интерактивный мастер Telegram запускается для каждой роли при обычной установке. Он предлагает включить уведомления, затем пошагово просит токен бота и числовой Chat ID (для групп — ID с минусом), проверяя формат каждого значения и повторяя запрос при ошибке. Токен получают через `@BotFather`, а Chat ID — через `@userinfobot` или настройки группы. В режиме `--unattended` запросы пропускаются: значения нужно безопасно заполнить позже в соответствующем env-файле.

## Unattended-режим

Для автоматизированного развертывания используйте явную роль:

```bash
sudo ./install.sh --role primary --unattended
sudo ./install.sh --role secondary --unattended
```

В этом режиме установщик не запрашивает интерактивные значения и предупреждает о неготовых ключах или параметрах. Не используйте auto-detect в CI: передавайте `--role` явно. `--no-systemd` устанавливает файлы, но не включает и не запускает unit/timer.

## Эксплуатация

- Текущее состояние: `sudo xui-standby status` или `sudo xui-standby status --json`.
- Ручной план: `sudo xui-standby plan --json`.
- Ручная синхронизация: `sudo xui-standby sync --json`.
- Проверка определённого архива: `sudo xui-standby check-backup --backup /opt/xui-backups/incoming/<archive>.gpg`.
- Логи: `journalctl -u xui-standby-sync.service -f`, `journalctl -u xui-backup.service -f`.
- Аварийное переключение: `sudo xui-failover status`, `sudo xui-failover promote`, `sudo xui-failover standby`.

Не переводите Standby в `PROMOTED` во время запуска репликации. В режиме, отличном от `STANDBY`, синхронизация блокируется, чтобы предотвратить split-brain.
