# TODO — аудит `xui_standby_sync`

## Шаг 1. Аудит первичный

На уровне Шага 1 обнаружено расхождение, требующее детального аудита на следующих шагах:

- `database.md` требует `sqlite3.connect(..., isolation_level=None)` и явные `BEGIN IMMEDIATE TRANSACTION` / `COMMIT` / `ROLLBACK`.
- В `engine.py` фактически присутствует `isolation_level=None`, но используются `BEGIN IMMEDIATE;`, `connection.commit()` и `connection.rollback()`.
- Комментарии в `engine.py` утверждают обратное — использование default isolation level.

Это уже относится к потенциальному риску **«Логика / Контракты»**, но подробная оценка требует продолжения аудита кода и тестов.

## Шаг 2. Аудит системного контура

Аудит выполнен в режиме read-only. Ниже перечислены замечания в формате: файл → строка → категория риска → рекомендация.

### `secondary-node/xui_standby_sync/security.py`

- `security.py` → 37–39 → **Безопасность** → Проверка через `lstat()`/`path.is_symlink()` и последующая работа с путем не образуют атомарной операции; при наличии конкурентного процесса возможна TOCTOU-подмена файла между проверкой и чтением/очисткой. Использовать уже открытый дескриптор с `O_NOFOLLOW`, проверить `fstat()` дескриптора и выполнять операции через дескриптор либо повторно проверять inode перед записью/удалением.
- `security.py` → 70–72 → **Безопасность** → Проверка каталога также разделена между `lstat()` и дальнейшим использованием пути; существует окно symlink/rename-атаки. Для критичных каталогов открывать родительский каталог с `O_DIRECTORY|O_NOFOLLOW` и применять descriptor-relative операции либо обеспечить эксклюзивную защиту дерева.
- `security.py` → 103–112 → **Безопасность** → `verify_directory_chain()` вызывает `path.absolute()`, а не канонизацию каждого компонента; проверка symlink-компонентов выполняется отдельными `lstat()`, но не защищена от конкурентной замены. Устранить TOCTOU через `openat2()` с `RESOLVE_NO_SYMLINKS` на Linux либо descriptor-relative traversal.
- `security.py` → 114–125 → **Безопасность** → `resolve()` следует по symlink до проверки принадлежности root; это корректно для статической проверки, но не защищает последующий `rmtree()`/доступ от race. Проверять и удалять объект через защищенный дескриптор/атомарный quarantine-путь.
- `security.py` → 137–139 → **Безопасность** → `path.exists()` и проверки symlink/type выполнены до `shred` неатомарно; атакующий может заменить путь после проверки. Передавать shred только после проверки inode и защищать каталог от конкурентной подмены.
- `security.py` → 161–166 → **Безопасность** → Fallback выделяет `b"\\x00" * size`, что может вызвать существенное потребление памяти/DoS на большом файле. Перезаписывать потоковыми блоками фиксированного размера.
- `security.py` → 167–171 → **Безопасность** → При ошибке нулевой перезаписи выполняется `unlink()` без подтверждения успешного затирания; для секретного файла это может оставить данные восстановимыми. Не удалять файл при неудачном wipe, возвращать ошибку/CRITICAL и сохранять доказуемо безопасный cleanup-путь.
- `security.py` → 137–171 → **Контракты** → `best_effort_wipe_file()` не проверяет владельца, права и дерево предков перед destructive-операцией. Явно ограничить функцию разрешенными рабочими каталогами и regular-file inode, а ошибки очистки не скрывать для чувствительных артефактов.

### `secondary-node/xui_standby_sync/locks.py`

- `locks.py` → 77–83 → **Безопасность / Логика** → Исключения `TypeError` и общий `OSError` при `fcntl.flock()` подавляются через `pass`; функция может вернуть дескриптор без реально установленной блокировки. Обрабатывать только `EWOULDBLOCK`/`EAGAIN` как busy, остальные ошибки пробрасывать как `SecurityViolationError`/`LockBusyError`.
- `locks.py` → 64–75 → **Безопасность** → Для существующего lock-файла проверяется только отсутствие group/world bits, но не точная маска `0600` и не владелец root для sync lock. Установить и проверять точные `0600 root:root` для sync lock; для общего store lock отдельно зафиксировать согласованный контракт `0600 xbackup:xbackup` и не смешивать его с root-only политикой.
- `locks.py` → 64–75 → **Гонки** → Проверка пути через `path.is_symlink()` перед `os.open()` дублирует защиту, но сама неатомарна; надежность зависит от наличия `O_NOFOLLOW`, который может отсутствовать на платформе. При отсутствии `O_NOFOLLOW` необходимо явно завершать работу, а не продолжать небезопасный open.
- `locks.py` → 87–104 → **Логика** → При исключении после добавления sync-дескриптора `close()` корректно выполняет LIFO; однако `time.sleep()` не прерывается callback/сигналом и при большом числе попыток задерживает освобождение sync lock. Ограничить retry policy и использовать прерываемое ожидание.
- `locks.py` → 106–125 → **Безопасность** → `close()` подавляет почти любые ошибки разблокировки/закрытия и не сообщает, удалось ли снять lock. Для аварийного cleanup допустим best-effort, но нужно логировать/возвращать диагностический результат; отдельно гарантировать освобождение при частично инициализированном `_handles`.
- `locks.py` → 87–104 → **Гонки / Контракты** → Общий `/opt/xui-backups/.store.lock` действительно синхронизируется через advisory `flock` с `xbackup`, только если receiver также блокирует тот же inode и использует совместимый механизм. Это необходимо подтвердить по полному коду receiver/install; иначе защита от гонки не доказана.

### `secondary-node/xui_standby_sync/archive.py`

- `archive.py` → 72–87 → **Безопасность** → Allowlist имен эффективно блокирует path traversal и hardlink/device-атаки через требование regular file; однако безопасность зависит от полного отсутствия альтернативных имен в allowlist. Сохранить строгий allowlist и дополнительно проверять normalized POSIX path без `..`/absolute компонентов.
- `archive.py` → 91–112 → **Безопасность / Логика** → `manifest_source.read()` читает manifest без лимита размера; архив может содержать огромный JSON и вызвать memory DoS. Ограничить размер manifest до малой фиксированной квоты и читать потоковыми блоками.
- `archive.py` → 113–135 → **Безопасность** → `copyfileobj()` не контролирует фактический объем извлеченного потока относительно `member.size`; защита ограничивает только заявленный размер tar-заголовка. Ввести счетчик фактически записанных байтов и завершать с ошибкой при превышении лимита.
- `archive.py` → 128–135 → **Гонки** → Создание destination через `O_EXCL` защищает от повторной записи, но parent `work_dir` и его компоненты могут быть подменены между проверкой/созданием и open. Использовать защищенное создание рабочего дерева и `O_NOFOLLOW` для destination.
- `archive.py` → 140–142 → **Контракты** → Обрыв/повреждение потока может дать `EOFError`, `zlib.error` или иное исключение декодера, тогда как worker явно перехватывает только `OSError`, `TarError`, `ArchiveValidationError`. Добавить согласованный перехват `EOFError` и проверить фактические типы исключений используемой версии Python; неизвестные ошибки также должны давать контролируемый failure result.
- `archive.py` → 156–161 → **Безопасность** → После `terminate()` worker ожидается только 5 секунд; если процесс не завершился, он может остаться жить и удерживать ресурсы/файлы. После timeout применить гарантированное убийство процесса (например, `kill()`), затем дождаться завершения и закрыть queue.
- `archive.py` → 153–169 → **Безопасность / Логика** → Используется multiprocessing context `fork`; worker наследует дескрипторы и состояние родительского процесса. Для root-сервиса предпочтительнее `spawn` либо явно закрывать ненужные дескрипторы и фиксировать допустимую модель изоляции.
- `archive.py` → 213–226 → **Безопасность** → `gpg-agent` завершается в `finally`, что соответствует требованию, но ошибка `CommandError` подавляется полностью; отказ cleanup не виден в диагностике и может оставить агент/секреты. Логировать failure без раскрытия секретов и проверять, что `GNUPGHOME` принадлежит ожидаемому дереву.
- `archive.py` → 228–230 → **Безопасность** → `payload_path` очищается best-effort; при неудаче wipe текущая реализация `security.py` может удалить файл без успешного затирания. После исправления wipe сделать failure cleanup явным и аварийным.

### `secondary-node/xui_standby_sync/config.py`

- `config.py` → 54–55 → **Контракты** → Проверка `str(path.resolve()).startswith("/etc/")` использует строковый prefix и некорректно классифицирует пути вроде `/etcetera/...`; применять `Path.relative_to(Path("/etc"))`.
- `config.py` → 66–68 → **Безопасность** → Парсер принимает значение до конца строки и удаляет только внешние кавычки; shell-комментарии, escape-последовательности и несбалансированные кавычки не валидируются. Это не shell injection при `subprocess(shell=False)`, но создает неоднозначные значения конфигурации. Либо поддержать строгий dotenv-синтаксис, либо отклонять спецслучаи и несбалансированные кавычки.
- `config.py` → 75–88 → **Безопасность / Логика** → Аннотация требует `str|None`, но runtime-проверки типов нет: `None` обработан, однако `int`, list или другой объект вызовет `AttributeError` на `.strip()`/`.split()` вместо контролируемого `ConfigurationError`. В начале каждого parser helper проверять `isinstance(value, str)` либо принимать только безопасный `None|str` boundary.
- `config.py` → 90–108 → **Безопасность / Логика** → Та же проблема для fingerprints/bool/int: неожиданный тип приводит к необработанным `AttributeError`/`TypeError`. Нормализовать входы и конвертировать все ошибки типов в `ConfigurationError`.
- `config.py` → 123–129 → **Контракты** → `load_values()` использует `path.is_file()` до `_parse_env_file()`; symlink на конфигурацию может пройти предварительную проверку, хотя затем проверяется разрешенный target. Зафиксировать запрет symlink и атомарно проверять владельца/режим файла перед чтением.
- `config.py` → 41–57, 151–152 → **Контракты** → Конфигурация путей из `docs/deployment.md` (`STANDBY_MODE_FILE`, `LOCK_FILE`, `TARGET_DB_PATH`, `INCOMING_DIR`, `ALLOWLIST_PATH`, `GNUPGHOME`, `SNAPSHOTS_DIR`, `WORK_SYNC_DIR`) не входит в `_ALLOWED_ENV_KEYS` и фактически игнорируется; модуль использует hardcoded constants. Либо обновить внешний контракт документации/install, либо реализовать строго проверяемые overrides для путей.
- `config.py` → 41–57, 176–225 → **Контракты** → Документированные параметры retention (`MAX_AGE_DAYS`, `MAX_SIZE_GB`, `KEEP_MIN_ARCHIVES`) отсутствуют, а пакет использует `MAX_AGE_SECONDS` и другие ключи. Синхронизировать имена и единицы измерения между systemd/env/bash-контрактами.

## Итоговые статусы

- `security.py` → **Дефекты обнаружены**.
- `locks.py` → **Дефекты обнаружены**.
- `archive.py` → **Дефекты обнаружены**.
- `config.py` → **Дефекты обнаружены**.

## Приоритет исправлений

1. Не подавлять ошибки `fcntl.flock()` в `locks.py`.
2. Устранить TOCTOU и небезопасный fallback удаления в `security.py`.
3. Ограничить фактический объем extraction/manifest и гарантировать завершение worker в `archive.py`.
4. Синхронизировать контракт переменных окружения и добавить runtime type validation в `config.py`.
5. Отдельно подтвердить по полному receiver-коду, что `xbackup` блокирует тот же `/opt/xui-backups/.store.lock` до всех операций публикации.

## Шаг 3. Аудит ядра данных и транзакционности

Аудит выполнен в режиме строго read-only. Файлы кода не изменялись. Формат: файл → строка → риск → рекомендация.

### `secondary-node/xui_standby_sync/database.py`

- `database.py` → 19–23 → **Безопасность / Контракт** → `connect_read_only()` корректно использует URI `mode=ro` после проверки файла; однако между `verify_file_security()` и `sqlite3.connect()` остается TOCTOU-окно, аналогичное уже зафиксированному для `security.py`. Для критичной БД открывать защищенный дескриптор с `O_NOFOLLOW` либо обеспечить descriptor-relative открытие и проверку inode.
- `database.py` → 53–61 → **SQL-безопасность** → `SQL_IDENTIFIER_RE` проверяет имя таблицы перед f-string в `PRAGMA table_info()`, поэтому явной SQL-инъекции через `table_name` не выявлено. При этом защита реализована только локально для этой функции; аналогичная функция `_columns()` в `engine.py` такой проверки не имеет.
- `database.py` → 26–39 → **Логика** → `verify_integrity()` выполняет `PRAGMA integrity_check` в read-only соединении, но имеет дублирующую проверку результата после `try` (строки 38–39); это не уязвимость, но усложняет контроль потока и диагностику. Удалить дублирование при исправлении.
- `database.py` → 117–160 → **Контракт схемы** → валидируются только наличие таблиц/минимальных колонок и allowlist-идентификаторы; не проверяются типы колонок, внешние ключи, уникальные индексы (`uuid`, `tag`) и совместимость DDL. При изменении схемы возможен формально прошедший, но небезопасный план. Добавить проверку фактического DDL/индексов согласно `docs/database.md`.
- `database.py` → 163–197 → **Целостность** → `get_invariants_snapshot()` и `verify_invariants()` читают `settings`, но не вызывают `verify_integrity()` и не проверяют наличие/валидность обязательных host-invariant ключей до формирования плана. Выполнять integrity check и валидировать полный набор `webPort`, `subPort`, `subURI`, `tgBotEnable` и локальные сертификатные ключи по контракту узла.

### `secondary-node/xui_standby_sync/planner.py`

- `planner.py` → 77–80 → **Read-only / Гонки** → обе базы открываются через `connect_read_only()`, DML в planner не обнаружен; требование нулевой модификации выполняется на уровне SQLite. Однако fingerprint снимается только с target до длительного чтения, поэтому изменения source во время планирования не фиксируются и план может быть построен по несогласованному снимку. Использовать согласованный snapshot/WAL-политику и повторно проверять источник перед применением.
- `planner.py` → 113–129 → **Host identity** → для Inbound 1 переносится весь JSON `settings` из source, а защита ограничивается сохранением `stream_settings`; явной проверки `webPort`, `subPort`, `subURI`, `tgBotEnable=false` и локальных `*CertFile/*KeyFile` здесь нет. Разделять клиентский массив от host-specific параметров и явно запрещать перенос host identity.
- `planner.py` → 132–201 → **Host identity / Сетевой риск** → для secondary inbounds новый объект формируется из allowlist source и допускает перенос `port`, `stream_settings` и прочих полей без проверки локальных ограничений, кроме reserved ports. Нужны явные проверки портов, protocol/listen, externalProxy и запрет локальных Reality/TLS-параметров там, где они являются host-specific.
- `planner.py` → 220–285 → **Dual-Layer Merge** → клиенты сопоставляются и планируются отдельно от `inbounds.settings`; при update/delete клиента JSON-массив не пересчитывается из фактического результата merge. Если allowlist/ключи/локальные ID отличаются, таблица `clients` и JSON `settings.clients` могут стать несогласованными. Формировать оба слоя из одного нормализованного набора клиентов и валидировать эквивалентность перед возвратом плана.
- `planner.py` → 226–230 → **Логика идентификации** → словарь `target_by_key` молча перезаписывает предыдущую запись при дубликате ключа или конфликте uuid/email. До построения плана необходимо отклонять дубликаты matching/fallback keys как `PlanValidationError`.
- `planner.py` → 276–285 → **Mass deletion / Целостность** → формируется полный список `client_deletes`, но отсутствует требуемый BR-404 guard: удаление более 50% клиентов Standby не блокируется. Добавить порог относительно текущего target и отдельное критическое исключение.
- `planner.py` → 345–367 → **Host identity** → произвольные `allowed_keys` из allowlist передаются из source в target; если allowlist содержит host-specific ключи, planner их не отфильтрует. Применять hard deny-list к `webPort`, `subPort`, `subURI`, `tgBotEnable`, сертификатам и прочим локальным параметрам независимо от конфигурационного файла.
- `planner.py` → 369–397 → **Контракт настроек** → `xrayTemplateConfig` обрабатывается вне `allowed_keys` и изменяет `routing.rules`/`outbounds`; отсутствует проверка host-specific полей и типов `target_xray.routing`. Некорректная структура target может вызвать ошибку или привести к нежелательному изменению конфигурации. Валидировать обе структуры и разрешать только документированные поля.
- `planner.py` → 249–259, 298–316 → **Логика ссылочной целостности** → проверка допускает unmapped source inbound, если его ID входит в `planned_source_ids`, но затем оставляет исходный ID вместо гарантированно разрешенного target ID. При создании нового inbound mapping появляется только в engine после INSERT, поэтому контракт должен явно различать source ID и target ID и запрещать неразрешенные ссылки.

### `secondary-node/xui_standby_sync/engine.py`

- `engine.py` → 31–33 → **Транзакционность / Контракт** → фактическое `isolation_level=None` и явный `BEGIN IMMEDIATE` соответствуют спецификации, но комментарий утверждает обратное и вводит в заблуждение аудитора/сопровождающего. Исправить комментарий; добавить обязательные `PRAGMA foreign_keys = ON` и `PRAGMA busy_timeout = 5000`.
- `engine.py` → 18–19 → **SQL-безопасность** → `_columns()` вставляет `table` через f-string без `SQL_IDENTIFIER_RE`. Сейчас вызываются только с константными именами, но функция не закрепляет этот инвариант. Использовать общий валидатор идентификаторов из `database.py` или исключить динамический SQL.
- `engine.py` → 65–77 → **Dual-Layer Merge** → обновление `inbounds.settings` выполняется отдельной операцией до client DML; атомарность транзакции есть, но engine не проверяет, что JSON-массив действительно соответствует последующим insert/update/delete в `clients`. Возможен commit несогласованных слоев при дефектном/скомпрометированном `DatabaseSyncPlan`. Перед BEGIN/COMMIT проверять контракт dual-layer.
- `engine.py` → 94–105, 126–137 → **Транзакционность** → для UPDATE проверка `cursor.rowcount == 0` реализована корректно и это покрывает требование. Однако `settings` UPDATE/INSERT (`160–171`), xray UPDATE (`173–177`) и все DELETE (`107–109`, `139–158`) не проверяют затронутые строки; устаревший или подмененный план может завершиться без фактического изменения. Проверять rowcount/существование каждой целевой записи.
- `engine.py` → 145–158 → **Целостность / Безопасность удаления** → перед удалением inbound не проверяется, что это не Inbound 1 и не локальный protected inbound; engine доверяет только planner/plan. Защитные инварианты должны быть enforced повторно в executor, иначе прямой вызов функции с валидным по типам, но опасным планом удалит рабочую конфигурацию.
- `engine.py` → 179–182 → **Обработка исключений** → `except BaseException` откатывает транзакцию, что полезно для аварийного прерывания, но скрывает контракт конкретных ошибок и противоречит стандарту проекта о перехвате целевых исключений. Откатывать на `sqlite3.Error`, `OSError`, `PlanExecutionError`, `OperationCancelledError` и отдельно безопасно обрабатывать системное прерывание.
- `engine.py` → 178–184 → **Надежность** → результат `COMMIT` не сопровождается post-commit integrity/invariant check; эти проверки должны быть выполнены до commit по BR-503, а после commit полезно подтвердить integrity для recovery/health-контуров. Добавить явную проверку плана и инвариантов до COMMIT.

### `secondary-node/xui_standby_sync/models.py`

- `models.py` → 16–172 → **Иммутабельность** → `frozen=True` делает dataclass-поля неизменяемыми только поверхностно; поля `Mapping[str, JSONValue]` могут ссылаться на обычные dict/list и изменяться после создания операции. Нормализовать nested JSON в immutable representation (`MappingProxyType`, tuple) либо deep-copy/freeze на границе конструирования.
- `models.py` → 102–149 → **Контракт операций** → frozen dataclasses не проверяют диапазоны/знаки ID, непустые имена ключей, JSON-валидность `settings_json`, допустимость `Mapping` и отсутствие `id` в insert values. Скомпрометированный или вручную созданный plan может пройти `is_valid` без semantic validation. Добавить `__post_init__`/фабрики валидации.
- `models.py` → 158–174 → **Контракт `DatabaseSyncPlan`** → `is_valid` проверяет только пустоту `validation_errors`; не проверяются взаимная согласованность `inbound_mapping`, операции удаления, уникальность IDs, обязательный Inbound 1 и dual-layer merge. Сделать проверку структурного контракта плана обязательной до executor.
- `models.py` → 11–13 → **Типы JSON** → `JSONValue` допускает mutable `list` и `dict`, поэтому сама аннотация противоречит заявленной неизменяемости. Ввести отдельные immutable JSON-типы или документировать глубокое копирование и гарантировать его runtime.

## Итоговые статусы шага 3

- `database.py` → **Дефекты обнаружены**: read-only и identifier regex присутствуют, но нет полной схемной/инвариантной валидации и устранения TOCTOU.
- `planner.py` → **Критические дефекты обнаружены**: host identity не enforced независимо от allowlist, dual-layer merge не доказан, отсутствует BR-404.
- `engine.py` → **Критические дефекты обнаружены**: базовая транзакция реализована правильно (`isolation_level=None` + `BEGIN IMMEDIATE` + `COMMIT/ROLLBACK`), но отсутствуют проверки части DML и повторное enforcement защитных инвариантов.
- `models.py` → **Дефекты обнаружены**: frozen dataclasses поверхностны, а semantic validation плана отсутствует.

## Приоритет исправлений шага 3

1. Исправить dual-layer merge: единый нормализованный набор клиентов, атомарное обновление таблицы и JSON, pre-commit consistency assertion.
2. Ввести независимое enforcement host identity и запрет удаления Inbound 1/protected inbounds в engine.
3. Добавить BR-404 mass-deletion guard и проверки дубликатов matching keys.
4. Сохранить явную транзакционность engine, добавить `foreign_keys`, `busy_timeout`, проверки rowcount и integrity/invariant assertions до COMMIT.
5. Углубить immutable/semantic validation моделей и убрать небезопасный f-string путь `_columns()`.

## Шаг 4. Аудит оркестрации, отката и жизненного цикла

Аудит выполнен в режиме строго read-only. Формат: файл → строка → категория риска → рекомендация.

### `secondary-node/xui_standby_sync/workflow.py`

- `workflow.py` → 85–170 → **Жизненный цикл / Критический риск** → После `stop_service()` нет безусловного `finally`, запускающего `start_service()`. Если snapshot, повторное планирование, merge, integrity/invariant check или сам `start_service()` завершаются исключением, сервис может остаться остановленным; при исключении до `service_stopped=True` аварийное завершение также не гарантирует восстановление. Ввести явную фазу recovery с флагом «сервис был остановлен», гарантировать `systemctl start` в `finally` после rollback/проверок и отдельно фиксировать неуспешный restart.
- `workflow.py` → 145–166 → **Обработка сигналов / Надежность** → `except Exception` не перехватывает `KeyboardInterrupt` и `SystemExit`; SIGINT/SIGTERM во время merge могут выйти из workflow без контролируемого результата и без гарантированного восстановления/старта. Добавить согласованный механизм cancellation/deferred cancellation: до остановки — clean non-zero/130/143, после остановки — сначала rollback/start, затем возврат POSIX-кода.
- `workflow.py` → 107–125 → **SQLite/WAL / Контракт** → Порядок `stop_service()` → `create_rollback_snapshot()` соблюден, но код не проверяет, что сервис действительно остановлен и WAL сброшен, а snapshot creation не выполняет отдельный `PRAGMA integrity_check` созданного snapshot. Зафиксировать проверяемый контракт остановленного unit и валидировать snapshot до destructive merge.
- `workflow.py` → 145–166 → **Откат / Логика** → При ошибке запуска после успешного commit workflow повторно восстанавливает pre-sync snapshot, хотя исходные данные уже применены; это может откатить успешную репликацию и маскировать первичную ошибку старта. Разделить ошибки до commit и post-commit lifecycle, определить policy восстановления и обязательно сообщать, что target уже изменен.
- `workflow.py` → 157–162 → **Обработка ошибок** → При восстановлении перехватывается только `SyncError`; `OSError`, `sqlite3.Error` и неожиданные ошибки recovery могут выйти из обработчика и не сформировать `ExecutionResult`. Нормализовать ошибки восстановления в `RollbackError`/`ServiceControlError`, сохранить исходную ошибку и обеспечить финальный cleanup locks.

### `secondary-node/xui_standby_sync/rollback.py`

- `rollback.py` → 112–131 → **SQLite/WAL / Критический риск** → Существующие `-wal`/`-shm` удаляются только после `os.replace()`. До удаления stale WAL может быть сопоставлен с уже замененным target и проигран SQLite, поэтому атомарность swap не равна атомарности восстановления. Сначала эвакуировать sidecar-файлы в защищенное уникальное quarantine-имя после остановки сервиса, затем заменить базу и удалить/secure-cleanup эвакуированные sidecars.
- `rollback.py` → 102–116 → **Гонки / Безопасность** → Временный путь `${target_db}.rollback.tmp` фиксирован и создается через `shutil.copy2()` без `O_EXCL`; конкурентный запуск или symlink подмена может перезаписать/подменить файл восстановления. Создавать уникальный временный файл в проверенном каталоге с exclusive create, `O_NOFOLLOW`, корректным owner/mode и cleanup в `finally`.
- `rollback.py` → 73–92 → **Целостность** → Snapshot создается через `sqlite3.Connection.backup()`, но после копирования проверяется только security/permissions; `PRAGMA integrity_check` применяется к target после restore, а сам snapshot до публикации не валидируется. Немедленно проверять snapshot read-only через `PRAGMA integrity_check` и не публиковать/не использовать поврежденный snapshot.
- `rollback.py` → 130–136 → **Обработка ошибок** → `verify_integrity()` может выбросить `IntegrityCheckError`/другой `SyncError`, но обработчик ловит только `(OSError, RollbackError)`, поэтому контракт функции «ошибка восстановления → RollbackError» нарушается. Перехватывать и нормализовать предусмотренные integrity/database ошибки без blind catch.
- `rollback.py` → 42–53, 93–98 → **Изоляция / Гонки** → Проверка `snapshots_dir` и последующий `mkdir`/`iterdir`/`rmtree` разделены TOCTOU; нет явного запрета, что `SNAPSHOTS_DIR` совпадает с `WORK_SYNC_DIR` или находится внутри очищаемого временного дерева. Проверять canonical boundaries и descriptor-relative дерево; явно исключить snapshots root из cleanup/retention временных каталогов и не удалять symlinked run directories.
- `rollback.py` → 49–53 → **Безопасность cleanup** → Ошибка `shutil.rmtree()` при ротации подавляется, поэтому старые snapshot-каталоги могут оставаться бесконтрольно и заполнять диск. Логировать failure и выдавать контролируемый operational alert/ошибку при нарушении retention.

### `secondary-node/xui_standby_sync/service.py`

- `service.py` → 14–18 → **Обработка таймаутов / Контракт** → `stop_service()` и `start_service()` преобразуют timeout `CommandError`, но `wait_service_active()` напрямую вызывает `run_command()`; timeout/ошибка запуска `systemctl is-active` не переводится в `ServiceControlError` и может прервать recovery неожиданным типом. Обрабатывать `CommandError` явно и отличать inactive от transport/timeout failure.
- `service.py` → 20–32 → **Надежность / Таймаут** → Polling использует полный `min(5, timeout)` на каждой итерации и `time.sleep(1)`, не учитывая оставшееся время; фактическое ожидание может превысить заданный timeout, а ожидание не имеет явного cancellation path. Передавать remaining timeout в команду, использовать прерываемое ожидание и гарантировать верхнюю границу.
- `service.py` → 8–32 → **Сигналы / Жизненный цикл** → Модуль не устанавливает обработчики SIGTERM/SIGINT и не предоставляет cancellation/deferred-recovery contract. При сигнале во время stop/start/poll cleanup workflow не может гарантировать корректную последовательность и POSIX-код 143/130. Определить единый signal controller на уровне CLI/service и не прерывать критическую recovery-фазу.

### `secondary-node/xui_standby_sync/status.py`

- `status.py` → 16–30 → **Read-only / Критический риск** → `_lock_busy()` открывает lock через `path.open("a+")`; это создает отсутствующий lock-файл, то есть `status` не является строго read-only. Использовать `os.open(..., O_RDONLY|O_NOFOLLOW)` только для существующего файла либо считать отсутствие свободным без создания.
- `status.py` → 24–27 → **Блокировки / Логика** → Для инспекции применяется `LOCK_EX|LOCK_NB`, временно запрашивая эксклюзивную advisory lock. Это может конкурировать с рабочим процессом и само изменяет поведение блокировки; status должен использовать read-only/non-invasive probe и явно учитывать несовместимость flock на разных inode.
- `status.py` → 35–43 → **Read-only / Гонки** → `exists()/is_file()/is_symlink()` и последующее чтение marker разделены TOCTOU; target DB integrity также проверяется отдельным read-only подключением без согласованного snapshot. Для статуса использовать descriptor-based reads/`O_NOFOLLOW`, а SQLite открыть URI `mode=ro` и явно документировать допустимую несогласованность WAL-состояния.
- `status.py` → 45–50 → **Контракт ошибок** → `run_command(systemctl is-active)` находится вне error boundary; timeout/ошибка запуска выбросит `CommandError`, вместо возврата диагностического `xui_service_active: unknown`. Обработать `CommandError` и различать inactive, timeout и execution failure.

### `secondary-node/xui_standby_sync/cli.py`

- `cli.py` → 84–95 → **Сигналы / POSIX-коды** → Нет обработчиков SIGTERM/SIGINT и нет преобразования cancellation в 143/130. Сейчас SIGINT/SIGTERM могут завершить Python напрямую, не обеспечив rollback/start, а `run_sync()` при обычной ошибке всегда возвращает код 1. Зарегистрировать handlers на границе CLI, передать cancellation в workflow, вернуть 130 для SIGINT и 143 для SIGTERM после завершения recovery.
- `cli.py` → 84–95, 105–156 → **Контракт CLI / Надежность** → Ветка `status` выполняется до общего `try`, поэтому ошибки конфигурации, `systemctl` и части чтения состояния не превращаются в гарантированный ненулевой CLI-код/JSON-ошибку. Поместить все подкоманды в единый error boundary и формализовать 0 только для успешных операций и clean standby guard.
- `cli.py` → 128–151 → **Откат / Контракт** → Для `--run-id` строится путь `snapshots_dir/snapshot-{run_id}.db`, тогда как `create_rollback_snapshot()` создает `snapshots_dir/{run_id}/target-before-sync.db`; штатный rollback по run-id не может найти созданный snapshot. Разрешать только валидированный run-id и разрешать его в фактический run directory/`target-before-sync.db`.
- `cli.py` → 134–148 → **Жизненный цикл / Rollback** → Rollback использует прямые `run_command(systemctl stop/start)` вместо `stop_service()`/`start_service()`: старт не проверяет `is-active`, а исключения/таймауты не нормализуются в единый recovery-контракт; при ошибке после stop сервис может остаться остановленным. Использовать общие lifecycle helpers и безусловный recovery start в `finally` с явным результатом.
- `cli.py` → 125–151 → **Безопасность / Контракт** → Marker режима читается без `verify_file_security()` и без проверки symlink/owner/mode, а `snapshot_path` из CLI передается в restore после лишь косвенной проверки внутри функции. Применять тот же защищенный marker validation и строгую проверку snapshot boundary/run-id до остановки сервиса.
- `cli.py` → 151–154 → **Отчетность** → Успешный rollback payload выводит `str(args.snapshot)`, поэтому при использовании `--run-id` поле `snapshot` равно `None`, а фактический restored path не отражается. Возвращать канонический фактически восстановленный snapshot и run-id для аудируемости.

## Итоговые статусы шага 4

- `workflow.py` → **Критические дефекты обнаружены**: отсутствует безусловный finally-start, нет signal/cancellation-контракта, recovery и post-commit ошибки разделены некорректно.
- `rollback.py` → **Критические дефекты обнаружены**: stale WAL/SHM очищаются после swap, snapshot не валидируется до использования, фиксированный temp path небезопасен.
- `service.py` → **Дефекты обнаружены**: polling не ограничен строгим deadline, timeout/error `is-active` не нормализуется, signal contract отсутствует.
- `status.py` → **Дефекты обнаружены**: status создает lock-файлы и берет EX-lock, нарушая strict read-only и non-invasive inspection.
- `cli.py` → **Критические дефекты обнаружены**: отсутствуют POSIX signal codes/recovery handlers, status вне error boundary, `--run-id` несовместим с layout snapshots.

## Приоритет исправлений шага 4

1. Спроектировать workflow recovery state machine с deferred SIGTERM/SIGINT и безусловным start после остановки.
2. Исправить WAL/SHM evacuation до restore swap и добавить integrity check snapshot до использования.
3. Устранить создание lock-файлов в `status` и нормализовать ошибки/таймауты systemctl.
4. Исправить разрешение `--run-id` и объединить rollback CLI с общим service lifecycle contract.
5. Зафиксировать boundary snapshots/work directories и сделать retention failures наблюдаемыми.
