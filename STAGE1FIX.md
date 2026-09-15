# Stage 1 — исправление системного контура безопасности

Исправлены замечания из `TODO.md` в четырех модулях пакета `xui_standby_sync`.

## Выполненные изменения

- `locks.py`
  - Ошибки `fcntl.flock()` больше не подавляются: `EWOULDBLOCK`/`EAGAIN` преобразуются в `LockBusyError`, прочие ошибки — в контролируемое нарушение безопасности.
  - Добавлена обязательная проверка наличия `O_NOFOLLOW`.
  - Зафиксирована точная политика `0600` и владельцев: `sync.lock` — `root:root`, общий `store.lock` — `xbackup:xbackup`.
- `security.py`
  - Критичный wipe начинается с открытия через `O_NOFOLLOW` и проверки inode через `fstat()`.
  - Fallback-затирание выполняется потоковыми блоками по 64 KiB без больших аллокаций.
  - При ошибке затирания файл не удаляется; возвращается `SecurityViolationError`.
- `archive.py`
  - Для `manifest.json` установлен лимит 1 MiB.
  - Извлечение базы контролирует фактический объем данных через ограниченный reader и `copyfileobj()`.
  - Добавлена обработка `EOFError`, `zlib.error` и гарантированное `kill()` worker после неудачного `terminate()`.
  - Ошибка `gpgconf --kill gpg-agent` теперь логируется в `finally`.
- `config.py`
  - Проверка принадлежности `/etc` выполняется через `Path.relative_to()`.
  - Добавлена runtime-проверка типов конфигурационных значений и строгая проверка кавычек.
  - Поддержан `MAX_AGE_DAYS` с переводом в секунды; одновременная передача `MAX_AGE_DAYS` и `MAX_AGE_SECONDS` отклоняется.

## Проверки

- `uv run --with ruff ruff check secondary-node/xui_standby_sync/archive.py secondary-node/xui_standby_sync/config.py secondary-node/xui_standby_sync/locks.py secondary-node/xui_standby_sync/security.py` — **успешно**.
- `uv run --with mypy mypy --exclude "tests" secondary-node/xui_standby_sync/` — **успешно**, `Success: no issues found in 20 source files`.
- Полный прогон тестов: **144 passed, 73 failed**. Оставшиеся сбои затрагивают существующие тестовые ожидания и модули других этапов (в частности `planner.py`, `rollback.py`, `status.py`, `workflow.py`); эти файлы намеренно не изменялись согласно ограничению этапа.
