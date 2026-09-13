# System Architecture Specification

## 1. Стек технологий

- **Data & Logic Layer:** Python 3.10+ (встроенные библиотеки `sqlite3`, `json`, `hashlib`, `pathlib`). Сторонние зависимости в production runtime запрещены.
- **Control & Network Layer:** GNU Bash 5.0+ (строгий режим `set -Eeuo pipefail`), `iptables` (DNAT/MASQUERADE), `systemd` (cgroups v2, песочницы).
- **СУБД:** SQLite 3.31+ в режиме журналирования WAL (`journal_mode=WAL`, `synchronous=NORMAL`).
- **Безопасность и транспорт:** OpenSSH (chroot/forced commands), GnuPG (GPG AES-256), `flock`, `shred`.

## 2. Топология узлов и распределение ролей

### Primary Node (Активный узел)

- Роль: Активный мастер обработки клиентских сессий и генерации данных.
- Процессы:
  - Сервис 3x-ui (Web UI: порт `39284`, Подписки: порт `39285`, Reality: порт `443`).
  - Локальный исходящий шлюз wireproxy/WARP (`127.0.0.1:40000`).
  - Таймер создания бэкапа (`xui-backup.timer`).
- Права: Выполнение конвейера бэкапа от `root`.

### Secondary Node (Резервный узел / Standby & Storage)

- Роль: Приемник архивов нулевого доверия, L4 DNAT-шлюз и горячий резервный инстанс.
- Расположение: Европейский ЦОД (прямой доступ к внешним API без проксирования).
- Процессы:
  - Изолированный SSH-приемник (`xui-backup-receiver.sh`) под системным пользователем `xbackup` (UID 999).
  - Фоновая санация и ротация хранилища (`xui-backup-retention.sh`).
  - Read-only сенсор SLA (`xui-backup-health.sh`).
  - Движок репликации клиентов (`xui-standby-sync`).
  - Локальный сервис 3x-ui (Web UI: порт `60291`, Подписки: порт `2096`, Reality: порт `443`).
  - L4 DNAT в таблице NAT: перенаправление внешнего входящего трафика на Primary Node в штатном режиме.

## 3. Внешние интерфейсы

- **DNS / Edge Layer:** Управление A-записями в режиме DNS-Only. Точки входа клиентов направлены на публичный IP Secondary Node.
- **Telegram Bot API:** Прямая отправка уведомлений о состоянии бэкапов и ошибках репликации.
