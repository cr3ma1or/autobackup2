● <analysis>
  Аудит выполнен без изменений исходного кода, конфигураций и
  документации. Создать `ROLES.md` сейчас нельзя: последнее
  ограничение запрещает любые файловые операции.

  1. Роли и install.sh

  Статус: частично корректно; есть несколько существенных
  расхождений.

  - install.sh:135-170 принимает --role primary, --role secondary
    и вариант с =; недопустимые значения отклоняются.
  - При интерактивном запуске роль выбирает оператор — это
    соответствует модели.
  - При unattended-запуске install.sh:260-290 роль определяется по
    остаточным Secondary-файлам:
    - /opt/xui-backups;
    - /etc/x-ui/sync.env.
      Если их нет, установщик молча выбирает primary. На новом
      Secondary, запущенном без --role secondary, будет развернут
      Primary. Для строгой ролевой модели unattended-режим должен
      требовать явный --role, а не угадывать роль.
  - На Primary ставятся backup/restore-скрипты, ключи,
    конфигурация и xui-backup.timer.
  - На Secondary ставятся receiver, retention, health-check,
    Python sync engine, failover CLI и Secondary timers.
  - Однако Secondary также получает primary-node/xui-backup.sh и
    primary-node/xui-restore.sh, а также unit/timer локального
    backup. Таймер выключается до promote. Это выглядит как
    намеренная DR-подготовка для превращения Secondary в Primary,
    а не случайная инверсия ролей, но это исключение из строгого
    правила «на узле только компоненты своей роли».
  - При повторном запуске с другой ролью установщик не очищает
    артефакты противоположной роли. Бывший Secondary может
    сохранить standby-marker, receiver и sync timers после
    установки как Primary; обратная ситуация аналогична.
  - install.sh:837-845 создаёт /etc/x-ui/standby-mode со значением
    STANDBY только на Secondary. Это корректно.
  - secondary-node/xui_standby_sync/workflow.py:80-90 разрешает
    синхронизацию только при маркере STANDBY и блокирует её при
    наличии failover-lock. Это соответствует изоляции standby.
  - На Primary явный role-marker не создаётся. Это не обязательно
    ошибка, но значит состояние роли Primary определяется
    отсутствием Secondary-артефактов, а не явным состоянием.
  - Secondary требует SSH-приём архивов, но install.sh:303
    устанавливает openssh-client, не openssh-server. Также шаблон
    secondary-node/examples/sshd_config_xbackup существует, но
    установщик его не разворачивает; вместо этого лишь выводит
    оператору инструкцию добавить ключ. Это делает чистую
    установку Secondary неполной.

  2. Документация и место выполнения promote

  Статус: роли описаны согласованно; смысловой инверсии в
  failover-инструкции нет.

  - README.md:119-124 предписывает выполнять sudo xui-failover
    promote на Secondary.
  - docs/failover.md:24-48 описывает ту же последовательность: при
    недоступности Primary команда запускается на Secondary.
  - docs/architecture.md:10-36 описывает Primary как активный
    рабочий узел, а Secondary — как gateway, archive receiver и
    горячий резерв.
  - Реализация подтверждает документацию:
    secondary-node/xui-failover.sh:424-439 останавливает Secondary
    sync, очищает DNAT, пишет PROMOTED, включает backup timer и
    перезапускает локальный x-ui. Следовательно, promote должен
    выполняться на Secondary, а не на Primary.

  Проблемы документации:

  - Корневой README.md находится на уровень выше каталога docs; ссылки
    из документов docs/ должны использовать `../README.md`.
  - docs/architecture.md:26 и docs/deployment.md:16 утверждают,
    что xbackup имеет UID 999. Установщик создаёт пользователя
    через useradd -r без -u 999, поэтому конкретный UID не
    гарантирован.
  - Документация содержит конкретные топологические параметры —
    порты, WARP/wireproxy, пути и расписания. Это допустимо как
    пример развёртывания, но не как полностью универсальная
    документация.

  3. Приватные данные, провайдеры и публичные адреса

  Статус: секретов и реальных публичных IP в tracked-коде не
  найдено, но обезличивание неполное.

  Остатки реальных инфраструктурных привязок:

  - docs/failover.md ранее содержал provider-specific названия вместо ролей Primary и Secondary.
  - Историческая ссылка на recensia.md также связывала роли с provider-specific названиями; в рабочей документации используются Primary и Secondary.
  - Если аудит включает локальную Git-метаинформацию,
    .git/config:7-9 раскрывает GitHub namespace в URL origin. Это
    не tracked-исходник и не попадает в распространяемый проектный
    контент, но остаётся локальным следом.

  Не обнаружено в tracked-файлах:

  - реальных публичных IPv4-адресов;
  - личных доменов;
  - Telegram bot tokens;
  - GPG private keys;
  - SSH private keys.

  Что найдено и является штатным/безопасным:

  - example.com, alerts.example.com, admin@example.com —
    корректные плейсхолдеры;
  - 192.0.2.1 и 192.0.2.2 в тестах — RFC 5737 documentation
    addresses, не реальные адреса;
  - 10.0.0.1 и 10.0.0.2 в примерах Python-модуля — private
    addresses, не утечка, но для строгого обезличивания лучше
    заменить на <PRIMARY_IP> и <STANDBY_IP>;
  - https://api.telegram.org/... в primary-node/xui-backup.sh:715,
    secondary-node/xui-failover.sh:346 и
    secondary-node/xui_standby_sync/notifications.py:21 — штатный
    endpoint интеграции; токен читается из конфигурации, а не
    зашит в репозитории;
  - wireproxy/WARP в docs/architecture.md, docs/deployment.md и
    docs/monitoring.md — внешняя технология, но не персональные
  - wireproxy/WARP в docs/architecture.md, docs/deployment.md и
    docs/monitoring.md — внешняя технология, но не персональные
    данные или провайдер.

  4. Хардкодинг в рабочей логике

  Статус: логика не опирается исключительно на аргументы, .env и
  автоопределение; системные параметры и policy-defaults широко
  зашиты в код.

  Primary:

  - primary-node/xui-backup.sh:89-103 фиксирует пути к .env,
    transfer-конфигурации, базе, backup-каталогу, логам, GPG-home
    и lock-файлам.
  - Там же зашиты policy-параметры: retention 14 дней, минимум 3
    архива, размер 2 GiB, stale work 6 часов.
  - Сетевые параметры доставки, включая host, user, port, key и
    known_hosts, загружаются из backup-transfer.env; это
    корректно.
  - primary-node/xui-restore.sh:58-68 фиксирует пути, имя x-ui,
    таймауты и параметры ожидания сервиса.

  Secondary:

  - secondary-node/xui-backup-receiver.sh:46-62 фиксирует storage
    диска, число загрузок и таймауты.
  - secondary-node/xui-backup-retention.sh:44-53 фиксирует пути,
    lock wait 7200 секунд, минимум архивов, default retention 14
    дней и срок invalid-архивов 7 дней. Только MAX_AGE_DAYS можно
    настроить через отдельный config-файл.
  - secondary-node/xui-backup-health.sh:36-38 фиксирует путь и
    допустимый возраст backup 26 часов.
  - secondary-node/xui_standby_sync/constants.py:5-27 содержит
    жёсткие дефолтные пути, lock-файлы, x-ui как имя сервиса и
    лимиты.
  - Python-конфигурация допускает переопределение ряда путей,
    лимитов, IP и GPG-параметров через /etc/x-ui/.env,
    /etc/x-ui/sync.env и окружение. Но имя сервиса, часть
    lock-paths и общий storage-layout остаются частью кода.

  Вывод: сетевые идентификаторы, IP и секреты в целом вынесены в
  конфигурацию. Однако пути, имена systemd units, service name,
  пользователь, лимиты, расписания и operational policy являются
  жёсткими предположениями проекта, а не полностью параметризуемой
  конфигурацией.
  </analysis>

  <summary>
  Ролевая модель в документации и failover-коде согласована:
  Primary активен, Secondary является gateway/standby, а `promote`
  запускается на Secondary. Основные проблемы — небезопасный
  auto-detect роли с default-to-primary, неполная чистая установка
  Secondary по SSH, сохранение артефактов при смене роли, неверно
  документированный UID `999`, provider-specific обозначения ролей и
  широкое использование жёстко заданных системных параметров.