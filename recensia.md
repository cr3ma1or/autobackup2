# Рецензия актуального состояния проекта

Проверка выполнена на ветке `audit/codebase-stabilization` по рабочему коду и документации. Ниже вердикты относятся к состоянию **до правок этой рецензии**; в конце каждого применимого пункта указаны внесённые исправления.

## 1. Роли серверов в `failover.md`

**[ОПРОВЕРГНУТО]** Роли не перепутаны. `docs/failover.md`, `README.md`, `docs/architecture.md`, `docs/deployment.md` и `secondary-node/xui-failover.sh` согласованы: VDSina — Primary, Aeza — Secondary. В штатном `STANDBY` Secondary транзитирует трафик на Primary; при аварии Secondary переводится в `PROMOTED`.

`sudo xui-failover promote` должен выполняться на **Secondary (Aeza)** от `root`. Это подтверждает и установщик: при `--role secondary` он устанавливает `xui-failover` в `/usr/local/bin/xui-failover`, а скрипт управляет standby marker, DNAT и локальными таймерами Secondary.

## 2. Утилита `xui-failover`

**[ОПРОВЕРГНУТО]** Компонент физически присутствует: `secondary-node/xui-failover.sh`. В `install.sh` он устанавливается для роли Secondary как `/usr/local/bin/xui-failover`. Реализованы команды `status`, `promote`, `standby`, блокировки, управление DNAT, таймерами и `x-ui.service`.

## 3. Тесты и релизные файлы

**[ОПРОВЕРГНУТО]** Упомянутые в `README.md` артефакты существуют и отслеживаются Git:

- Python-тесты: `secondary-node/xui_standby_sync/tests/`;
- Bash-набор: `test_bash_suite.sh`;
- заметки релиза: `RELEASE_NOTES.md`.

В корне отдельной папки `tests/` нет, однако `README.md` на неё не ссылается: он указывает фактический вложенный путь.

## 4. SQLite-транзакции Python-пакета

**[ОПРОВЕРГНУТО]** Рабочая запись в `engine.py` открывается через `sqlite3.connect(target_db, isolation_level=None)`. Далее выполняются явные `BEGIN IMMEDIATE;`, `COMMIT;` и `ROLLBACK;`.

`database.py` открывает базы только для чтения через URI `mode=ro`; там режим записи и границы транзакций не применяются. В `rollback.py` используются стандартные подключения SQLite только для SQLite Backup API, а не для merge-транзакции.

Также удалён устаревший комментарий в `engine.py`, ошибочно описывавший default `isolation_level`.

## 5. Расписание синхронизации и возраст бэкапа

**[ПОДТВЕРЖДЕНО]** До правки синхронизатор принимал архивы лишь до `6 * 3600` секунд (`21600`), и то же значение было в `sync.env.example`. Это не совместимо с фактической схемой: Primary создаёт архив ежедневно в `03:20 UTC` с задержкой до 20 минут, а installer запускает sync в `04:00` и `16:00 UTC` с задержкой до 15 минут. Второй запуск может получить архив почти суточной давности.

**Исправлено:** `DEFAULT_MAX_AGE_SECONDS` и `MAX_AGE_SECONDS` шаблона приведены к `93600` (26 часов); `docs/business-rules.md` описывает это окно. Кроме того, пример `secondary-node/examples/xui-standby-sync.timer` синхронизирован с installer: `04:00` и `16:00 UTC`, а не прежние 2 часа.

## 6. Mass-Deletion Guard

**[ЧАСТИЧНО]** Проверка в `planner.py` уже была: она блокировала удаление более 50% managed-клиентов. Однако условие `managed_target_count > 1` пропускало удаление единственного клиента: $1 / 1 = 100\%$.

**Исправлено:** защита теперь действует при любом ненулевом количестве managed-клиентов. Добавлен тест, подтверждающий блокирование удаления единственной записи.

## 7. Диагностические коды `xui-backup-health.sh`

**[ПОДТВЕРЖДЕНО]** `docs/monitoring.md` не соответствовал скрипту.

Фактические причины включают:

- `missing_dependency`, `bash_too_old`, `incoming_directory_missing_or_unreadable`, `no_archives_found`;
- `unparseable_timestamp`, `invalid_timestamp_epoch`;
- `archive_or_sidecar_missing_or_empty`, `checksum_fail`;
- `archive_vanished_during_check`, `invalid_archive_size`;
- `unexpected_error`, `interrupted`.

При устаревшем, но целостном архиве скрипт возвращает `STATUS=WARN`, код `1` и `reason=stale`. Это не `CRITICAL` и не `stale_backup`. При успехе он выводит `STATUS=OK ... checksum=OK` **без** поля `reason=`.

**Исправлено:** контракт, коды возврата и реестр причин в `docs/monitoring.md` обновлены по фактическому скрипту.

## 8. Telegram-уведомления синхронизации

**[ПОДТВЕРЖДЕНО]** До правки `send_telegram()` была объявлена в `notifications.py`, конфигурация собиралась в `config.py`, но `workflow.py` и `cli.py` её не вызывали. Поэтому сообщения о результате синхронизации не отправлялись.

**Исправлено:** `workflow.py` вызывает `send_telegram()` best-effort при успешной синхронизации, dry-run и контролируемой ошибке. Ошибка доставки уведомления не меняет результат синхронизации. Документация `architecture.md` и `failover.md` обновлена.

## 9. Мелкие детали и контракты

### Receiver: regex и порядок аргументов

**[ПОДТВЕРЖДЕНО]** `docs/business-rules.md` содержал старый протокол `xui-backup-put` и другой порядок параметров. Реальный receiver принимает:

`receive <archive_name> <sha256_lowercase> <size_bytes>`

с regex:

`^receive[[:space:]]+(xui-backup-[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9]+\.tar\.gz\.gpg)[[:space:]]+([a-f0-9]{64})[[:space:]]+([1-9][0-9]{0,12})$`

**Исправлено:** BR-301 обновлён.

### Имя rollback-снимка

**[ПОДТВЕРЖДЕНО]** Фактическое имя в `rollback.py` и `cli.py` — `target-before-sync.db`, не `last-good-pre-sync.db`.

**Исправлено:** `docs/features.md` и `docs/ux-guidelines.md` обновлены.

### Карантин повреждённых архивов

**[ПОДТВЕРЖДЕНО]** Формат не соответствует описанию `.corrupt.<timestamp>`. Receiver использует `${archive}.partial.<random>` для неполного чтения, `${archive}.badsha256` для неверной суммы и `${archive}.${suffix}.<random>` для других сбоев. Retention при санации применяет отдельные форматы `${archive}.${timestamp}.orphaned` и `${archive}.${timestamp}.corrupt`.

**Исправлено:** BR-302 описывает оба фактических механизма и лимиты хранения.

### Явная root-проверка Python sync

**[ПОДТВЕРЖДЕНО]** До правки в Python-коде не было `os.geteuid() == 0`; требование обеспечивалось только systemd/операционной процедурой.

**Исправлено:** `_verify_standby()` в `workflow.py` теперь прерывает sync без root-привилегий. Тесты workflow изолируют проверку через mock EUID. `docs/patterns.md` обновлён.

### `matching_key` из allowlist

**[ОПРОВЕРГНУТО]** `planner.py` действительно читает `matching_key` из `allowlist["tables"]["clients"]`; для traffic используется `allowlist["tables"]["client_traffics"]`. Для клиентов также применяется настроечный `fallback_matching_key` (по умолчанию `email`).

---

## Итог

Аудит на старой версии верно указал на ряд исторических расхождений документации и кода, но ошибся по ролям failover, существованию `xui-failover`, наличию тестовых/релизных файлов, транзакционному режиму engine и чтению `matching_key`.

Подтверждённые несоответствия документации исправлены; также устранены три практические проблемы кода: несогласованное окно свежести синхронизации, обход mass-deletion guard для одного клиента, отсутствие root-проверки и неиспользуемые Telegram-уведомления sync.
