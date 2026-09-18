# Регламент переключения 3x-ui

## 1. Назначение и роли

Регламент описывает безопасное переключение трафика между Primary (Active) и Secondary (Standby / Gateway). В каждый момент времени только один узел является активным источником данных и принимает боевой трафик.

- **STANDBY** — Secondary (Standby / Gateway) работает как резервный узел и L4-шлюз: входящие порты транзитируют на Primary (Active), а `xui-standby` принимает и применяет проверенные резервные копии.
- **PROMOTED** — Primary (Active) считается недоступным, Secondary становится активным узлом: транзит отключён, локальный 3x-ui обслуживает трафик, автоматическая синхронизация остановлена.
- **FAILBACK** — после восстановления Primary данные, созданные на Secondary, сначала переносятся на Primary через свежий backup/restore. Только после проверки Primary Secondary возвращается в `STANDBY`.

## 2. Штатная работа: STANDBY

1. Убедитесь, что маркер состояния и сервисы корректны:

   ```bash
   sudo xui-failover status
   sudo xui-standby status
   ```

2. Ожидаемое состояние: `Mode: STANDBY`, активен `xui-standby-sync.timer`, а `xui-backup.timer` на Secondary остановлен/отключён.
3. Не изменяйте локальную сетевую идентичность Secondary и не запускайте ручную синхронизацию одновременно с переходом роли.
4. Контролируйте журнал синхронизации и уведомления Telegram: workflow отправляет best-effort сообщение об успехе или ошибке. Ошибки подписи, возраста или целостности архива требуют расследования до аварийного переключения.

## 3. Авария Primary: PROMOTED

### 3.1. Подтверждение аварии

Перед переключением подтвердите, что Primary действительно недоступен или изолирован. По возможности заблокируйте его исходящий и входящий боевой трафик/остановите `x-ui.service`. Это обязательная защита от **Split-Brain**: нельзя продвигать Secondary, пока Primary может одновременно принимать записи или обслуживать клиентов.

Проверьте состояние Secondary:

```bash
sudo xui-failover status --json
sudo systemctl is-active xui-standby-sync.service
```

Если синхронизация выполняется, дождитесь её завершения или остановите процесс штатно. Не используйте `promote` параллельно с `xui-standby sync`.

### 3.2. Продвижение Secondary

Выполните на Secondary (Standby / Gateway) от имени `root`:

```bash
sudo xui-failover promote
sudo xui-failover status
```

Команда атомарно блокирует конкурентные переходы, останавливает `xui-standby-sync.timer` **и активный** `xui-standby-sync.service`, очищает DNAT-цепочку, сохраняет правила firewall, записывает `PROMOTED`, включает локальный `xui-backup.timer` и перезапускает `x-ui.service`. Перед `promote` заранее подготовьте на Secondary `/etc/x-ui/.env`, `primary-local-gnupg` и GPG recipients для DR-backup; installer не создаёт эти секреты автоматически. После проверки доступности сервисов сообщите о переходе и зафиксируйте время/причину инцидента.

Не запускайте `xui-failover promote` повторно без анализа текущего состояния. При подозрении на живой Primary сначала устраните его доступ к боевому трафику.

## 4. Работа в PROMOTED

- Secondary является единственным активным узлом; все изменения клиентов выполняются только на нём.
- `xui-standby` не должен синхронизировать входящие архивы, пока маркер `PROMOTED`.
- Следите за `xui-backup.timer`, свободным местом и журналами:

  ```bash
  sudo xui-failover status
  sudo systemctl list-timers xui-backup.timer
  sudo journalctl -u xui-backup.service -f
  ```

- Не возвращайте узел в `STANDBY`, пока восстановленный Primary не получил свежие данные и не подтверждена его готовность.

## 5. Восстановление Primary и FAILBACK

### 5.1. Подготовка и защита от Split-Brain

1. Восстановите Primary, но не публикуйте его боевые IP/маршруты до завершения реконсилиации.
2. Убедитесь, что Secondary остаётся единственным активным узлом (`PROMOTED`), пока готовится возврат.
3. На Secondary убедитесь, что `xui-backup.timer` сформировал свежий архив с данными, созданными во время аварии. Этот архив является источником для восстановления Primary.

### 5.2. Реконсилиация данных Secondary → Primary

На **Secondary (promoted)** найдите последний успешно созданный архив:

```bash
sudo systemctl start xui-backup.service
sudo find /backup/x-ui -maxdepth 1 -type f -name 'xui-backup-*.tar.gz.gpg' -printf '%T@ %p\n' | sort -nr | head -n 1
```

Безопасно передайте выбранный архив на Primary по защищённому каналу. На **Primary**, пока Secondary остаётся в `PROMOTED`, поместите архив в `/backup/x-ui/` и выполните проверенное восстановление:

```bash
sudo xui-restore --yes /backup/x-ui/<свежий-архив>.tar.gz.gpg
sudo systemctl status x-ui.service --no-pager
```

`xui-restore` проверяет подпись, manifest, хеши и SQLite перед заменой БД, сохраняет rollback-копию и перезапускает `x-ui.service`. После восстановления проверьте клиентов, inbound-настройки, порты и журналы. Если проверка не пройдена, не выполняйте failback и используйте rollback/устраните причину.

### 5.3. Возврат Secondary в STANDBY

После подтверждения, что Primary полностью готов, а переключение согласовано и его боевой трафик будет включён единственным владельцем роли, на Secondary выполните:

```bash
sudo xui-failover standby
sudo xui-failover status
```

Интерактивная команда требует точного ввода `CONFIRM_STANDBY`; для заранее согласованного автоматизированного окна допускается `sudo xui-failover standby --yes`. Команда останавливает и отключает локальный backup-таймер, восстанавливает DNAT на Primary, записывает `STANDBY`, а затем запускает таймер синхронизации.

### 5.4. Контроль после failback

На Primary:

```bash
sudo systemctl enable --now xui-backup.timer
sudo systemctl status xui-backup.timer --no-pager
```

На Secondary:

```bash
sudo xui-standby validate --json
sudo xui-standby status
sudo systemctl list-timers xui-standby-sync.timer
```

Ожидается: Primary обслуживает боевой трафик, Secondary имеет `STANDBY`, DNAT активен, `xui-backup.timer` на Secondary выключен, а синхронизация снова получает новые архивы. Любое расхождение ролей — повод немедленно остановить публикацию одного из узлов и расследовать состояние.

## 6. Аварийные правила

- Не удаляйте маркер `/etc/x-ui/standby-mode` вручную и не редактируйте его во время перехода.
- Не обходите блокировки и не запускайте одновременно `promote`, `standby` и `xui-standby sync`.
- Все решения о переключении фиксируйте: время UTC, подтверждённое состояние Primary, команду, результат и ответственного.
- При невозможности доказать, какой узел является активным, безопасное действие — изолировать оба узла от боевого трафика до установления единственного владельца роли.
