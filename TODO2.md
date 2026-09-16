# TODO2 — результаты изолированного E2E Smoke Test

Дата проверки: 2026-09-16
Скрипт: `e2e_smoke_test.py`
Песочница: `sandbox/` (только каталог проекта; production-каталоги `/etc/x-ui`, `/opt/xui-backups`, `/run` и systemd не использовались).

## 1. Результат smoke-теста

- CLI фактически проверен: `sync`, `plan`, `--dry-run`, `--backup`, `--json`.
- Сгенерирован SQLite donor/standby со схемой `inbounds`, `clients`, `client_traffics`, `settings`.
- Создан асимметрично зашифрованный GPG archive с `manifest.json` и SHA-256 sidecar.
- Plan/dry-run: PASS; запланировано `clients_insert=2`.
- Actual sync: PASS; exit code 0.
- Clients table: PASS.
- Dual-Layer (`clients` + `inbounds.settings.clients`): PASS.
- Host invariants `webPort=60291`, `subPort=2096`, `tgBotEnable=false`: PASS.
- Inbound 1 Reality public/private key identity: PASS.
- Сервисные операции и lock в тесте заменены локальными test doubles, поскольку WSL-аккаунт UID 1000 не должен обращаться к systemd и production lock paths.

## 2. Расхождения и потенциальные проблемы

### 2.1. Конфигурация путей

**Наблюдение:** пользовательский сценарий требует `.env` с переопределением путей в sandbox, однако `config.py` принимает только policy/security/notification keys, а пути жёстко заданы в `constants.py`.

**Файлы для правки:**
- `secondary-node/xui_standby_sync/constants.py`
- `secondary-node/xui_standby_sync/config.py`
- `secondary-node/xui_standby_sync/models.py`
- тесты `tests/test_config.py`

**План фикса:** добавить явно разрешённые `TARGET_DB_PATH`, `INCOMING_DIR`, `WORK_DIR`, `SNAPSHOTS_DIR`, `GNUPG_DIR`, lock/log/marker paths; валидировать, что overrides находятся в допустимом root; добавить тесты precedence и path traversal.

### 2.2. Формат архива и тестовый контракт

**Наблюдение:** реальная расшифровка требует GPG; при `REQUIRE_GPG_SIGNATURE=false` архив всё равно должен быть GPG-encrypted. В smoke harness использован локальный ephemeral RSA key без подписи.

**Файлы для правки:**
- `secondary-node/xui_standby_sync/archive.py`
- `secondary-node/xui_standby_sync/backup.py`
- `primary-node/xui-backup.sh`
- `tests/test_archive.py`

**План фикса:** документировать два поддерживаемых режима (encrypted-only и signed+encrypted), добавить интеграционный fixture для обоих режимов и явно проверять несовместимые комбинации конфигурации.

### 2.3. Reality identity не покрыта общим allowlist invariant

**Наблюдение:** текущий smoke-тест проверяет Reality keys вручную. `assert_invariants` работает только с `settings`, поэтому `inbounds.stream_settings` identity не защищается общим механизмом.

**Файлы для правки:**
- `secondary-node/xui_standby_sync/database.py`
- `secondary-node/xui_standby_sync/planner.py`
- `secondary-node/xui_standby_sync/workflow.py`
- `docs/business-rules.md`
- `tests/test_planner.py`, `tests/test_workflow.py`

**План фикса:** добавить структурированные invariant paths для inbound 1 Reality (network, keys, SNI, externalProxy, port) и проверять baseline до/после sync.

### 2.4. Snapshot lifecycle требует production lifecycle

**Наблюдение:** `workflow.py` создаёт snapshot после `stop_service`, что корректно, но E2E под UID 1000 не может подтвердить фактическое состояние systemd.

**Файлы для правки:**
- `secondary-node/xui_standby_sync/workflow.py`
- `secondary-node/xui_standby_sync/service.py`
- `tests/test_workflow.py`
- `docs/architecture.md`

**План фикса:** добавить отдельный privileged integration profile, который запускается только в disposable VM/WSL test image; оставить unit tests с mocks, но проверять порядок stop → snapshot → mutation → start.

### 2.5. Уязвимость/риск тестового harness

**Наблюдение:** ephemeral GPG private key хранится в `sandbox/gnupg`; harness должен удалять sandbox после успешного прогона или явно помечать `--keep` как режим диагностики.

**Файлы для правки:**
- `e2e_smoke_test.py`
- возможно `docs/monitoring.md` для CI cleanup policy

**План фикса:** добавить `try/finally`, secure wipe private key material (`shred` при наличии), автоматическое удаление sandbox по умолчанию и финальную проверку отсутствия секретных артефактов.

## 3. Приоритетный план исправлений

1. **P0 — конфигурация:** реализовать документированные path overrides и тесты безопасности путей.
2. **P0 — invariants:** расширить invariant engine на структурированные Reality-поля.
3. **P1 — archive contract:** добавить signed/encrypted integration fixtures и негативные тесты.
4. **P1 — lifecycle:** подготовить privileged disposable integration profile для проверки systemd/rollback/WAL.
5. **P2 — harness hygiene:** secure cleanup GPG material и sandbox artifacts.
6. **P2 — документация:** синхронизировать `docs/architecture.md`, `docs/business-rules.md`, README и operational runbook.
