# Changelog

## Партия 6: счётчик импорта + FloodWait + настройки + Управление устройствами (2026-04-25)

Большая партия по результатам реальных проблем заказчика: импорт TData
тихо проваливается без сообщения, нужна возможность управления чужими
сессиями отдельно от импорта, нужны настройки для всех таймаутов.

### Часть А — счётчик импорта TData (тихий провал больше не возможен)

**Проблема:** заказчик импортировал TData, в логе появлялись только Шаг 1
и Шаг 3, потом тишина. Никаких сообщений в GUI о провале — пользователь
не понимал что импорт не удался.

**Что сделано в `gui.py / _import_tdata`:**
- Перед запуском `tdata_thread` считается `accounts_before = len(db.get_all_accounts())`.
- В потоке после успешного `TDesktop()` сохраняется `tdata_state["expected"] = tdesk.accountsCount`.
- В `finally` потока пересчитывается `accounts_after`, вычисляется `added`,
  формируется итоговое сообщение из 4 типов:
    - `fail_early` (added=0, expected=0): «Импорт прерван до чтения TData»
    - `fail` (added=0, expected>0): «ИМПОРТ НЕ УДАЛСЯ: TData содержит N, добавлено 0»
    - `partial` (0<added<expected): «Импорт частичный: добавлено K из N»
    - `success`: «Импорт завершён: добавлено N»
- Сообщение идёт через `log_queue` с тегом `accounts_import_summary`.
- В `on_queue_message`: для fail* — `messagebox.showerror`, для partial —
  `messagebox.showwarning`, для success — только в лог.

### Часть Б — FloodWait retry с настраиваемыми лимитами

**Проблема:** на шагах 5/6 (connect/get_me) если Telegram возвращал FloodWait
на 30+ секунд, наш `asyncio.wait_for(timeout=30)` выбрасывал TimeoutError —
пользователь видел «ПРЕВЫШЕН ТАЙМАУТ» вместо честного «жди N сек».

**Что сделано в `gui.py`:**
- Хелпер `_try_with_flood_retry(coro_factory, max_wait_sec, jitter_min, jitter_max, log_cb)`.
  Один ретрай если `FloodWaitError.seconds <= max_wait_sec`. Иначе
  пробрасываем наружу с понятным сообщением. Между попыткой и ретраем —
  пауза `e.seconds + uniform(jitter_min, jitter_max)`.
- `client.connect()` и `client.get_me()` обёрнуты в эту функцию.
- В `except FloodWaitError` (если ретрай тоже не помог или ждать слишком долго):
  «FloodWait Nс — Telegram просит подождать. Подсказка: повтори через Nс
  или увеличь tdata_flood_max_wait_seconds в настройках».

### Часть В — все таймауты и паузы вынесены в настройки

**Что сделано в `ads_models.SchedulerSettings`** (новые поля):
- `tdata_connect_timeout_seconds` (default 60)
- `tdata_get_me_timeout_seconds` (default 60)
- `tdata_flood_max_wait_seconds` (default 300)
- `tdata_flood_jitter_min_seconds` (default 1)
- `tdata_flood_jitter_max_seconds` (default 3)
- `device_terminate_delay_min_seconds` (default 1)
- `device_terminate_delay_max_seconds` (default 3)
- `device_terminate_default_schedule_hours` (default 2)

**Что сделано в `ads_database.py`:**
- В `int_fields` добавлены 8 новых ключей для load_scheduler_settings.
- В save_scheduler_settings добавлены set_setting для всех 8.
- Миграция `_migrate_schema_v3` создаёт таблицу `pending_device_terminations`
  + индекс на (status, scheduled_at).

**Что сделано в `gui.py / SettingsFrame`:**
- Frame теперь прокручиваемый (CTkScrollableFrame) — настроек стало много.
- Три секции: «API-ключи (.env)», «Импорт TData (таймауты)», «Управление устройствами».
- В сохранении: API-ключи в .env, числовые — в БД через AdsDB.save_scheduler_settings.
- Валидация: int>=1, для пар min<=max.

### Часть Г — кнопка «Устройства» (отдельно от импорта)

**Что убрано:**
- Из `_import_tdata` удалён Шаг 7/8 «session hygiene». Импорт больше
  автоматически не убивает чужие сессии.

**Что добавлено:**
- В `AccountsFrame` toolbar новая кнопка «Устройства» (после «Прокси»).
- Метод `_open_devices`: в фоновом потоке подключается через `TelegramSender`,
  читает `account_manager.list_sessions(client)`, сериализует Authorization-объекты
  в dict (так как они не pickle-able через очередь), отправляет в `log_queue`
  с тегом `accounts_devices_loaded`.
- Класс `DevicesDialog` (~280 строк):
    - текущая сессия отдельной плашкой (read-only, нельзя убить);
    - чужие сессии в `CTkScrollableFrame` с чек-боксами;
    - сессии младше 24ч показываются с disabled чек-боксом и пометкой
      «(нельзя убить <24ч)» — Telegram запрещает ResetAuthorizationRequest для них;
    - кнопка «Выбрать все (можно убить)»;
    - радио-кнопки «Убить выбранные сейчас» / «Запланировать удаление через N мин/часов»;
    - дефолт радио = «запланировать», дефолт N = `device_terminate_default_schedule_hours`,
      ед. изм = «часов».
- Метод `_kill_now(hashes)`: фоновый поток, `terminate_specific_sessions(client, hashes)`.
- Метод `_schedule(hashes)`: пишет в `pending_device_terminations` через
  `add_pending_device_termination`.

**Новая функция `account_manager.terminate_specific_sessions`:**
- Принимает список конкретных hash'ей вместо «всех чужих».
- Настраиваемая пауза `delay_min_seconds`/`delay_max_seconds` через `random.uniform`.
- Возвращает `dict {killed, skipped, errors}`.
- FloodWait прерывает цикл (как и раньше).

### Часть Д — расписание удалений (Tk-таймер)

**Что сделано в `TeletonApp`:**
- При старте — `cleanup_old_device_terminations(30)` чистит записи `done`/`failed` старше 30 дней.
- Через 5с после старта — первый тик `_check_pending_device_terminations`.
- Тикает раз в 60с (`self.after(60000, ...)`). Не отдельный поток.

**Метод `_check_pending_device_terminations`:**
- Читает `get_due_device_terminations(now_iso)` из БД.
- Для каждой просроченной задачи запускает фоновый поток `terminate_thread`.

**Метод `_execute_pending_termination(task, settings)`:**
- В фоновом потоке подключается через `TelegramSender`, выполняет
  `terminate_specific_sessions(client, task["auth_hashes"])`.
- При успехе — `mark_device_termination_done(task_id)`.
- При ошибке — `mark_device_termination_failed(task_id, error[:500])`.

**Стратегия выполнения = вариант В + Tk-таймер:**
- Если GUI закрыт в момент `scheduled_at` — задача выполнится при следующем
  запуске GUI задним числом (через `cleanup` за 30 дней не успеет удалиться).
- Это правдоподобно: «пользователь пришёл, навёл порядок».

### CRUD-методы `pending_device_terminations` в `AdsDB`

- `add_pending_device_termination(account_phone, auth_hashes: list, scheduled_at_iso) -> int`
- `get_due_device_terminations(now_iso) -> list[dict]` — pending tasks где `scheduled_at <= now`
- `mark_device_termination_done(task_id)`
- `mark_device_termination_failed(task_id, error)` — error обрезается до 500 символов
- `cleanup_old_device_terminations(days_old=30)` — чистит только done/failed старше N дней

### Тесты

- `tests/test_pending_device_terminations.py` (21 тест):
  - схема v3 (таблица + индекс)
  - add: store_as_json, default_status_pending, empty_list
  - get_due: возврат просроченных, пропуск future/done/failed, сортировка ASC,
    обработка битого JSON
  - mark_done, mark_failed (с обрезкой до 500 символов)
  - cleanup: removes_old_done, keeps_recent_done, keeps_old_pending
  - load/save: дефолты, сохранение/чтение настраиваемых таймаутов

- `tests/test_flood_retry.py` (7 тестов):
  - первая попытка успешна → 1 вызов
  - короткий FloodWait → ретрай, всего 2 вызова
  - длинный FloodWait > лимита → пробрасываем без ретрая
  - FloodWait == max_wait_sec → ретрай (граница включена)
  - двойной FloodWait → пробрасываем после второго (ретрай только один)
  - другое исключение (TimeoutError) → пробрасываем без ретрая
  - log_cb получает сообщение о FloodWait

Прогон: **237/237 тестов зелёные** (209 предыдущих + 21 + 7 новых).

## Партия 5: подробное логирование импорта TData (2026-04-25)

**Контекст:** заказчик запустил импорт TData у себя. Лог обрывается на
строке "Конвертирую TData", дальше тишина — было невозможно понять, на
каком шаге процесс встал. Кроме того, при ошибках юзер видел только сухое
имя класса исключения без объяснения, что делать.

### Что сделано

**Пошаговое логирование (8 шагов)** в `gui.py / _import_tdata`:
- Шаг 1/8: pre-flight проверка папки TData (существует, есть key_datas + hex-папка)
- Шаг 2/8: проверка прав на запись в `data/sessions/` (write-test через временный файл)
- Шаг 3/8: чтение TData через opentele
- Шаг 4/8: ToTelethon (создание Telethon-клиента)
- Шаг 5/8: client.connect() с таймаутом 30с
- Шаг 6/8: client.get_me() с таймаутом 30с
- Шаг 7/8: сессионная гигиена (зачистка чужих сессий)
- Шаг 8/8: rename session-файла + запись в БД

Каждый шаг пишет начало и результат в файл-лог через `log_to_file` ещё ДО
выполнения операции — если процесс зависнет, в логе будет видно ровно
на каком шаге это случилось.

**Словарь подсказок `TDATA_ERROR_HINTS`** (модульный уровень):
переводит технические имена исключений в человеческое объяснение:
- `AuthKeyUnregisteredError` → "TData устарела или auth_key отозван..."
- `TimeoutError` → "Истёк таймаут. Скорее всего прокси не отвечает..."
- `OSError` → "Системная ошибка ввода-вывода. Возможные причины..."
- и ещё 11 типов исключений Telethon/opentele/системных

**Таймауты на сетевые операции:** `client.connect()` и `client.get_me()`
обёрнуты в `asyncio.wait_for(..., timeout=30)`. Раньше зависший прокси
держал процесс несколько минут; теперь — ровно 30 секунд, с понятным
сообщением "ПРЕВЫШЕН ТАЙМАУТ" и подсказкой про прокси.

**Стартовый маркер:** в начале `tdata_thread` пишется в лог-файл
"=== START tdata_thread, path=..., proxy=<set/none> ===".

### Как использовать при проблемах у заказчика

Попросить файл `data/logs/teleton_<date>.log`. Внутри увидите цепочку
`[~] Шаг N/8: ...` → `[+] Шаг N/8 ОК` для каждого этапа. Где обрывается
на `[~]` без `[+]` — там и проблема. Если есть `[-] Шаг N/8 ПРОВАЛЕН:
<ИмяОшибки>` — рядом будет строка `[-] Подсказка: <человеческий текст>`.

### Тесты

Новый файл `tests/test_tdata_error_hints.py` (11 тестов): покрытие словаря
подсказок, fallback-логика `_hint_for`, наличие подсказок для критичных
типов исключений.

## Партия 4: фиксы рандомных задержек (2026-04-25)

**Контекст:** в предыдущей партии (stage1-4) интегрировали рандомные паузы
между сообщениями в DM/mention/broadcast/group_check, плюс per-group
`next_allowed_at` в БД. При ревью нашли два бага.

### Фикс 1: Паузы при любом не-терминальном статусе

**Проблема:** в DM-цикле и broadcast-цикле паузы делались ТОЛЬКО при
`status == "sent"`. Если пачка получателей возвращала ошибки
(`private`, `error`, `no_permission`) — софт слал запросы один за
другим без пауз. Telegram видит шквал = поведенческий red flag.

**Что изменено в `gui.py`:**
- DM-цикл: пауза теперь после ЛЮБОГО статуса, кроме терминальных
  (`banned`, `flood_wait` → break без паузы).
- Mention-цикл: пауза теперь и в ветке `else` (errors), не только при `sent`.
- Broadcast-цикл: тот же подход + замена хрупкой проверки `task is not tasks[-1]`
  на `task_i < len(tasks) - 1` через `enumerate`.

### Фикс 2: Сброс `next_allowed_at` при изменении интервала группы

**Проблема:** юзер меняет в GUI `interval_minutes` группы с 6ч на 1ч.
Но публикация не идёт ещё 5 часов, потому что `next_allowed_at` в БД
был выставлен под старое значение интервала.

**Что изменено в `ads_database.update_group`:**
- Перед UPDATE читаются старые `interval_minutes` и `interval_minutes_max`.
- Если они изменились — `next_allowed_at` принудительно очищается в БД.
- Если интервал не менялся — `next_allowed_at` сохраняется как был.

### Фикс 3: Диапазон в логе при паузах

**Что изменено в `gui.py`:** во всех 4 точках логирования пауз
(DM/mention/broadcast/group_check) теперь показывается актуальный
диапазон: `Пауза 92с (диапазон 60-180с)`.

### Тесты

Новый файл `tests/test_update_group_resets_next_allowed.py` (5 тестов):
сброс при изменении min/max интервала, сохранение при изменении других
полей, идемпотентность при пустом next_allowed_at.

## Две партии рефакторинга (2026-04-24)

### Партия 1: разделение api_id (гибридная стратегия)

**Почему:** auth_key в TData выписан Telegram'ом под api_id=2040 (Telegram Desktop).
Старый код использовал один захардкоженный api_id для всего — `UseCurrentSession`
импортировал auth_key под Desktop, но последующие запросы шли с самописным api_id.
Это fingerprint-mismatch → риск `AUTH_KEY_UNREGISTERED` через несколько часов работы.

**Что сделано:**
- `config.py`: захардкоженные `API_ID`/`API_HASH` удалены. Вместо них:
  - `OWN_API_ID` / `OWN_API_HASH` читаются из `.env` (для phone-login и fallback);
  - `DESKTOP_API_ID` / `DESKTOP_API_HASH` — зашиты в код (только для TData-импорта).
- `models.Account`: добавлены поля `api_id`, `api_hash`, `device_model`,
  `system_version`, `app_version`, `lang_code`. У каждого аккаунта теперь
  свой набор api/device.
- `sender.TelegramSender._create_client`: читает api_id из `account`,
  fallback → `OWN_API_ID`. Если пусто в обоих местах — падает с понятной ошибкой.
- `gui._import_tdata`: при импорте TData в БД пишутся Desktop-значения
  (api_id=2040 + device_model="Desktop" + app_version="5.6.3 x64" + lang_code="ru").
- `database`: миграция v1 → v2 через `PRAGMA user_version`. Новые колонки
  добавляются идемпотентно через `ADD COLUMN IF NOT EXISTS`-эмуляцию.

### Партия 2: статус-машина аккаунтов + WAL + классификация ошибок

**Почему:** раньше `connect()` возвращал просто True/False. Все классы ошибок —
auth-fail, peer-flood, bann, сетевая проблема — смешивались в один `return False`.
Аккаунт оставался `is_active=1`, и каждый следующий цикл долбил мёртвую сессию.

**Что сделано:**
- `models`: 4 статуса — `active` / `needs_reauth` / `banned` / `network_issue`.
  Поля в `Account`: `status`, `flood_until`, `connect_fail_count`, `last_status_change`.
- `database`: миграция v2 → v3 добавляет новые поля + индексы
  (`idx_send_log_target_status`, `idx_send_log_timestamp`, `idx_parsed_users_group`).
  Включён WAL-режим (`PRAGMA journal_mode=WAL`).
- `database`: методы `on_connect_success` / `on_connect_network_issue`
  / `on_connect_error` / `set_account_status` / `set_account_flood_until`.
  Порог автопометки — 3 подряд неудачи (`CONNECT_FAIL_THRESHOLD`).
  Cooldown для `network_issue` — 5 минут (`NETWORK_RECOVERY_MINUTES`),
  после чего аккаунт автоматически пробуется снова.
- `database.get_active_accounts`: учитывает `status` и `flood_until`.
  `network_issue` попадает в выборку после истечения cooldown — sender
  пробует reconnect, при успехе `on_connect_success` возвращает в `active`.
- `sender.connect`: теперь ловит `AuthKeyUnregistered`, `SessionRevoked`,
  `UserDeactivatedBan`, `PhoneNumberBanned`, `ConnectionError`, `OSError`
  по отдельности → проставляет правильный статус.
- `sender.send_mention_message`/`send_dm`: при превышении retry-лимита
  `FloodWaitError` ставят `flood_until = now + e.seconds`. Аккаунт не попадёт
  в `get_active_accounts` до истечения flood'а.
- `ads_database`: включён WAL-режим (двойное соединение с Database больше
  не даёт `database is locked`).
- GUI: в таблице аккаунтов новая колонка **Статус** с индикацией flood-паузы.
  `_toggle_account` при включении вызывает полную реактивацию (сброс
  `status`, `connect_fail_count`, `flood_until`).

### Тесты

Добавлена папка `tests/` с 66 тестами (все зелёные):

- `test_migration.py` (8 тестов) — fresh install, миграция v1 → v3,
  идемпотентность, сохранение legacy-данных.
- `test_account_status.py` (18 тестов) — статус-машина, пороги
  автопометки, фильтрация в `get_active_accounts`.
- `test_sender_api_selection.py` (4 теста) — приоритет api_id,
  fallback на `OWN_API_ID`, дефолты device.
- `test_spintax.py` (13 тестов) — `spin_text`, `spin_unique`, `apply_mask`.
- `test_proxy_normalization.py` (15 тестов) — все 5 форматов прокси,
  edge cases по портам, SCHEME preservation.
- `test_parse_until_datetime.py` (8 тестов) — RU/EN/ISO форматы,
  прошедшие даты, невалидные даты.

Запуск: `pytest tests/ -v`

### Что НЕ входит в эту итерацию

Осталось на следующие партии:
- Phone-login flow (sign_in через phone/code/password)
- Шифрование `.session`-файлов
- Задержки между отправками в `cmd_mention` / `cmd_broadcast`
- Кольцевая ротация аккаунтов с учётом `flood_until`
- Миграционная инфраструктура для `ads_database`
- Удаление `.session` при `delete_account`
- Перенос проекта из OneDrive (это юзеру на уровне файловой системы)

### Обязательное действие перед запуском

В корне проекта создать файл `.env` из `.env.example`:
```
cp .env.example .env
```
Заполнить `OWN_API_ID` и `OWN_API_HASH` значениями с https://my.telegram.org.

Если старая БД уже существует (`data/teleton.db`) — она мигрирует автоматически
при первом запуске. Существующие аккаунты останутся с пустыми api_id/api_hash;
для их работы нужно либо переимпортировать через TData, либо заполнить
вручную через SQL:

```sql
UPDATE accounts
SET api_id=2040,
    api_hash='b18441a1ff607e10a989891a5462e627',
    device_model='Desktop',
    system_version='Windows 10',
    app_version='5.6.3 x64',
    lang_code='ru'
WHERE api_id=0;
```

Либо проще — удалить БД целиком:
```bat
del /q data\teleton.db
del /q data\sessions\*.session
del /q data\sessions\*.session-journal
```
и переимпортировать TData заново через GUI.
