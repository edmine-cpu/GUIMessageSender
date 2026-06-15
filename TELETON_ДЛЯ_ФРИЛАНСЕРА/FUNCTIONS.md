# Teleton — полное описание функций

Документация по всем модулям и функциям софта. Описано как они работают изнутри: какие проверки делают, какие условия отслеживают, в каком порядке выполняются.

---

## Содержание

1. [Архитектура и точки входа](#1-архитектура-и-точки-входа)
2. [Модели и БД](#2-модели-и-бд)
3. [Авторизация и подключение (sender.py + account_manager.py)](#3-авторизация-и-подключение)
4. [Раздел: Аккаунты](#4-раздел-аккаунты)
5. [Раздел: Парсинг](#5-раздел-парсинг)
6. [Раздел: Аудитории](#6-раздел-аудитории)
7. [Раздел: Рассылка](#7-раздел-рассылка)
8. [Раздел: Каналы](#8-раздел-каналы)
9. [Раздел: Автоответчик](#9-раздел-автоответчик)
10. [Раздел: Объявления (ads-планировщик)](#10-раздел-объявления)
11. [Раздел: Настройки](#11-раздел-настройки)
12. [Spintax + AI-уникализация](#12-spintax--ai-уникализация)

---

## 1. Архитектура и точки входа

### Главный класс `TeletonApp` (gui.py)

Окно приложения на customtkinter. При запуске:

1. **Загружает конфиг** — `Config.load()` читает `.env` и параметры по умолчанию.
2. **Создаёт sidebar** — кнопки разделов («Аккаунты», «Задачи», «Парсинг», «Аудитории», «Рассылка», «Каналы», «Автоответчик», «Аккаунт», «Объявления», «Статистика», «Настройки»).
3. **Создаёт content-area** — пустой контейнер, в который lazy-инициализируются Frame'ы при первом клике.
4. **Стартует `_poll_queue()`** — каждые 100 мс читает очередь сообщений от фоновых потоков и роутит их в обработчики Frame'ов.
5. **Чистит старую историю расписаний** — `cleanup_old_device_terminations(30)` удаляет записи `done`/`failed` старше 30 дней.
6. **Запускает Tk-таймер расписания** — через 5 секунд после старта вызывает `_check_pending_device_terminations`, потом каждые 60 секунд.

### Очередь логов `log_queue`

Фоновые потоки не могут напрямую обновлять GUI (это требование Tk). Поэтому используется паттерн «очередь сообщений»:

- В фоновом потоке: `log_queue.put((tag, msg))` — `tag` определяет в какой Frame идёт сообщение, `msg` — содержимое.
- В main-thread: `_poll_queue()` каждые 100 мс читает очередь и вызывает `frame.on_queue_message(tag, msg)`.

Теги используют префиксы: `accounts_*` → AccountsFrame, `parsing_*` → ParsingFrame, `audiences_*` → AudiencesFrame и т.д.

### Перехват `print()` через `_thread_local`

Внутри фоновых потоков `print(...)` перехватывается. Каждый поток ставит `_thread_local.log_handler = lambda m: log_queue.put(...)` и `_thread_local.log_tag = "accounts"`. Когда внутри потока вызывается `print()`, патченая версия пушит в очередь с правильным тегом. Это позволяет писать обычный код с `print()`, и он автоматически попадает в нужный лог-виджет.

---

## 2. Модели и БД

### `models.Account`

Один аккаунт Telegram. Поля:

- `phone` — номер телефона с `+`, primary key.
- `session_name` — путь к `.session`-файлу (без расширения).
- `proxy` — прокси в формате `scheme://user:pass@host:port` или пусто.
- `is_active` — пользователь включил/выключил.
- `sent_today` — счётчик отправленных сообщений за сегодня.
- `last_reset_date` — дата последнего сброса счётчика.
- `api_id`, `api_hash`, `device_model`, `system_version`, `app_version`, `lang_code` — параметры под которые был создан auth_key. Для TData это всегда `2040 / Desktop / Windows 10`.
- `status` — `active` / `needs_reauth` / `banned` / `network_issue`.
- `flood_until` — ISO-датайм до которого аккаунт под FloodWait.
- `connect_fail_count` — сколько раз подряд не смог подключиться (после 3 → автостатус `network_issue`).
- `last_status_change` — когда статус менялся последний раз.

### Статус-машина аккаунта (`database.py`)

При каждом подключении через `sender.connect()` вызывается один из методов БД:

- **`on_connect_success(phone)`** — обнуляет `connect_fail_count`, если статус был `network_issue` — возвращает в `active`.
- **`on_connect_network_issue(phone)`** — увеличивает счётчик. Если ≥ 3 — ставит `network_issue` и `flood_until = now + 5 минут` (cooldown). До истечения cooldown аккаунт не попадёт в `get_active_accounts()`.
- **`on_connect_error(phone, error_class)`** — для конкретных ошибок:
    - `AuthKeyUnregisteredError`, `SessionRevokedError` → `needs_reauth`.
    - `UserDeactivatedBanError`, `PhoneNumberBannedError` → `banned`.
    - Прочие — увеличивают `connect_fail_count`, но не меняют статус сразу.

### `database.get_active_accounts()`

Возвращает аккаунты пригодные для работы. Условия:

1. `is_active = 1`
2. `status = 'active'`
3. `flood_until` пустое **или** в прошлом (если в прошлом — статус не сбрасываем сразу, аккаунт сам пройдёт через `connect()` и при успехе вернётся в active).

Аккаунты `needs_reauth` / `banned` / `network_issue` (с действующим cooldown) — не попадают.

### WAL-режим SQLite

`database.py` и `ads_database.py` оба используют:
```sql
PRAGMA journal_mode=WAL
```

Без WAL основной код и ads-планировщик дрались за блокировку при записи. С WAL — записи идут в отдельный журнал, кассир (SQLite) разбирает их асинхронно. Результат — нет ошибки `database is locked` при параллельных операциях.

### `ads_database.AdsDB`

Параллельная БД для рекламного планировщика. Содержит таблицы:

- `ads` — тексты объявлений (с медиа).
- `groups_targets` — целевые группы (куда постить), с расписанием и статусом.
- `scheduler_settings` — пары ключ/значение, общие настройки (читается через `load_scheduler_settings()` → `SchedulerSettings`).
- `required_subs` — обязательные каналы для подписки перед публикацией.
- `publication_log` — лог публикаций (что, куда, когда, статус).
- `pending_device_terminations` — расписание удалений чужих сессий.

### Миграции

При открытии БД автоматически вызываются `_migrate_schema_v2`, `_migrate_schema_v3`. Они идемпотентны (повторный вызов — без эффекта), используют `ALTER TABLE ADD COLUMN IF NOT EXISTS`-эмуляцию через `PRAGMA table_info`. Старая v1 БД получает все новые колонки и таблицы автоматически без потери данных.

---

## 3. Авторизация и подключение

### `TelegramSender._create_client()` (sender.py)

Создаёт объект `TelegramClient` Telethon. Логика выбора api_id:

```
1. Если в Account.api_id есть значение → используем его (и Account.api_hash, device_model, ...)
   Это случай TData: 2040 / Desktop / Windows 10.

2. Иначе → fallback на OWN_API_ID / OWN_API_HASH из .env.
   Это для phone-login сессий, которые в этой версии не реализованы, но
   готовность к ним есть.

3. Если оба пусты → ValueError. Аккаунт нельзя использовать.
```

Это **гибридная стратегия**: TData-аккаунты ходят с api_id под который выписан их auth_key (2040), новые phone-login аккаунты — со своим api_id из `.env`. Никакого fingerprint mismatch.

### `TelegramSender._parse_proxy()` (sender.py)

Универсальный парсер прокси-строк. Поддерживает 5 форматов:

- `host:port`
- `host:port:user:pass`
- `user:pass@host:port`
- `scheme://user:pass@host:port` (`socks5://...`, `http://...`, `socks4://...`)
- `host:port:user:pass:scheme`

Возвращает кортеж `(scheme, host, port, user, pass)` — формат который понимает `python-socks` через `python-telethon`.

### `TelegramSender.connect()` (sender.py)

Главная функция подключения. Внутри — `_raw_connect_with_retry()` с тремя попытками для устойчивости к транзиентным сетевым ошибкам.

Алгоритм:

1. **Создание клиента** через `_create_client()`. Если `api_id` пустой — отбой, статус не меняется.
2. **Подключение** `client.connect()` — устанавливает MTProto-соединение.
3. **Проверка авторизации** `client.is_user_authorized()` — Telegram подтвердил что наш auth_key валиден?
    - Если нет → статус `needs_reauth`. Возвращает `False`.
    - Если да → продолжаем.
4. **`on_connect_success`** в БД — статус возвращается в `active`, счётчик ошибок обнуляется.

При ошибках:

- **`AuthKeyUnregisteredError` / `SessionRevokedError`** → `needs_reauth`, return False.
- **`UserDeactivatedBanError` / `PhoneNumberBannedError`** → `banned`, return False.
- **`ConnectionError` / `OSError`** → счётчик +1, после 3 — `network_issue` с cooldown 5 минут.
- **`FloodWaitError`** → ставит `flood_until`, return False.

### `TelegramSender.can_send_more()` (sender.py)

Проверяет дневной лимит. Если `Account.last_reset_date != today` — обнуляет `sent_today` и обновляет `last_reset_date`. Возвращает `sent_today < daily_limit` (из конфига).

### `account_manager.list_sessions(client)` (account_manager.py)

Через `GetAuthorizationsRequest` получает список всех активных сессий аккаунта. Это тот же список что в Telegram → Settings → Devices.

Возвращает список объектов `Authorization` (telethon), у каждого:

- `hash` — int, нужен для удаления.
- `current` — bool, это ТА сессия которой мы сейчас пользуемся (нельзя убить).
- `device_model`, `platform`, `system_version`, `app_name`, `app_version` — что за устройство.
- `ip`, `country`, `region` — откуда заходили.
- `date_created` — когда сессия создана (если <24ч — Telegram не даст её убить).
- `date_active` — последняя активность.

### `account_manager.terminate_other_sessions(client)` (account_manager.py)

Убивает все сессии кроме `current`. Между удалениями — рандомная пауза 1-2.5с (не настраиваемая, использовалась только в старой логике гигиены). При `FreshResetAuthorisationForbidden` (сессия <24ч) — пропускает с логом. При `FloodWaitError` — прерывает цикл.

### `account_manager.terminate_specific_sessions(client, auth_hashes, ...)` (account_manager.py)

Новая функция (партия 6). Убивает только перечисленные хеши. Параметры:

- `auth_hashes` — list[int], какие сессии убить.
- `delay_min_seconds`, `delay_max_seconds` — диапазон рандомной паузы между убийствами (читается из `SchedulerSettings`).

Возвращает `dict {killed, skipped, errors}`.

---

## 4. Раздел: Аккаунты

UI: `AccountsFrame` (gui.py).

### Кнопка «Вкл/Выкл»

Метод `_toggle_account()`. Переключает `Account.is_active` для выбранного аккаунта в таблице. Дополнительно при включении:

- Сбрасывает `status = 'active'`
- Обнуляет `connect_fail_count = 0`
- Очищает `flood_until = ''`

Это **полная реактивация**: если аккаунт был помечен как `network_issue` или `needs_reauth` от старой ошибки, и пользователь его сам включает — даём ему второй шанс пройти `connect()` заново.

### Кнопка «Удалить»

Метод `_delete_account()`. Удаляет запись из БД через `db.delete_account(phone)`. **Не удаляет** `.session`-файл с диска (это TODO).

### Кнопка «Импорт сессий»

Метод `_import_sessions()`. Открывает диалог выбора файла `.session`. Сначала пытается подключиться через сессию-файл, через Telethon достать `me.phone`, потом сохраняет в БД с `api_id = OWN_API_ID` (предполагается что это файл от приложения с твоим api_id).

В этой версии используется редко, основной импорт — TData.

### Кнопка «Импорт TData» — главный сценарий

Метод `_import_tdata()`. Открывает `ImportTDataDialog` → пользователь указывает путь к `tdata` и опционально прокси. Затем стартует `tdata_thread` в фоне.

**Алгоритм 8 шагов с подробным логированием:**

#### Шаг 1/8 — pre-flight проверка папки

- Проверяет `os.path.isdir(tdata_path)`. Если папки нет — выход с сообщением.
- Читает содержимое и проверяет:
    - Наличие файла `key_datas` или `key_datass`.
    - Наличие папки с hex-именем (16 символов A-F, 0-9), например `D877F783D5D3EF8C`.

Если хоть одно отсутствует — это не TData, выводим понятное сообщение «возможно вы указали родительскую папку, попробуйте указать вложенную tdata/».

#### Шаг 2/8 — проверка прав на запись

В `data/sessions/` создаётся временный файл `.write_test`, потом удаляется. Если падает с `OSError` — антивирус или нет прав. Понятное сообщение.

#### Шаг 3/8 — чтение TData через opentele

Вызывается `tdesk = TDesktop(tdata_path)`. Если падает — пишем в лог тип исключения + хинт через `TDATA_ERROR_HINTS`. Полный traceback в файле.

После успеха проверяем `tdesk.isLoaded() and tdesk.accountsCount > 0`. Если нет — TData повреждена / multi-account проблема.

**Важно:** запоминаем `tdata_state["expected"] = tdesk.accountsCount` — это число будет нужно в `finally` для подсчёта результата.

#### Шаг 4/8 — ToTelethon (для каждого аккаунта в TData)

`client = await tdesk.ToTelethon(session, flag=UseCurrentSession, api=DESKTOP_API)`. Создаёт Telethon-клиент с auth_key из TData, сохраняет .session-файл.

Проксирование: если пользователь указал прокси, парсится через `_parse_proxy` и передаётся в `ToTelethon`.

#### Шаг 5/8 — `client.connect()`

Обёрнут в `_try_with_flood_retry`:

- Таймаут из настроек: `tdata_connect_timeout_seconds` (default 60).
- Если `FloodWaitError.seconds <= tdata_flood_max_wait_seconds` (default 300) — ждём + ретрай.
- Если `> max_wait_sec` — пробрасываем без ретрая, пользователь видит «FloodWait Nс — повтори через Nс».
- Если просто `TimeoutError` — выход с подсказкой про прокси.

#### Шаг 6/8 — `client.get_me()`

Та же обёртка. После успеха:
- `phone = "+" + me.phone` или `tdata_<userId>` если телефона нет.
- Логируется в файл и в GUI.

**Шаг 7/8 — сессионная гигиена — УБРАН** в партии 6. Перенесён в отдельную кнопку «Устройства».

#### Шаг 8/8 — rename + запись в БД

- Переименовываем `session_tdata_<userId>.session` → `session_<phone>.session`.
- Если файл с таким именем уже есть — удаляется старый.
- В БД записывается новый Account с полями TData: `api_id=2040`, `device_model="Desktop"`, и т.д.
- Если phone уже существует в БД — обновляется (с сохранением `is_active`, `sent_today`, `last_reset_date` от старой записи).

#### `finally` — подсчёт результата

После завершения потока (успех или провал на любом шаге):

1. Считаем `accounts_after = len(db.get_all_accounts())`.
2. `added = accounts_after - accounts_before`.
3. Классификация:
    - `added <= 0, expected == 0` → `fail_early` («прерван до чтения TData»).
    - `added <= 0, expected > 0` → `fail` («не удался: содержит N, добавлено 0»).
    - `added < expected` → `partial` («частичный: K из N»).
    - `added >= expected` → `success`.
4. В очередь идёт сообщение с тегом `accounts_import_summary`. Обработчик в GUI:
    - `fail`/`fail_early` → `messagebox.showerror`.
    - `partial` → `messagebox.showwarning`.
    - `success` → только в лог, без модала.

### Кнопка «Прокси»

Метод `_set_proxy()`. Открывает диалог ввода прокси-строки. Сохраняет в `Account.proxy` для выбранного аккаунта. Не валидирует подключение — просто пишет строку в БД.

### Кнопка «Устройства» (партия 6)

Метод `_open_devices()`. Стартует `devices_thread` в фоне:

1. Подключается к выбранному аккаунту через `TelegramSender`.
2. Вызывает `account_manager.list_sessions(client)`.
3. Сериализует Authorization-объекты в dict (так как они не pickle-able через очередь).
4. Отправляет в очередь с тегом `accounts_devices_loaded`.

Обработчик в GUI открывает `DevicesDialog`. В диалоге:

- **Текущая сессия** — отдельной плашкой, read-only, чек-бокса нет.
- **Чужие сессии** — в `CTkScrollableFrame`, у каждой чек-бокс. Сессии <24ч имеют disabled чек-бокс и пометку «(нельзя убить <24ч)» (Telegram запрещает `ResetAuthorizationRequest` для свежих).
- **Кнопка «Выбрать все можно убить»** — массово выделяет чек-боксы у не-disabled сессий.
- **Радио «сейчас / запланировать»** — дефолт = «запланировать».
- **Поле N + ед. изм. (минуты/часы)** — дефолтное N = `device_terminate_default_schedule_hours` (2).

При нажатии «Применить»:

- **Сейчас** → `_kill_now(hashes)` стартует `kill_thread`, вызывает `terminate_specific_sessions` с паузой `device_terminate_delay_min/max_seconds`.
- **Запланировать** → `_schedule(hashes)` записывает в `pending_device_terminations` через `add_pending_device_termination`.

### Tk-таймер расписания удалений

В `TeletonApp` каждые 60 секунд тикает `_check_pending_device_terminations`:

1. Читает `get_due_device_terminations(now)` — задачи с `status='pending'` и `scheduled_at <= now`.
2. Для каждой запускает `_execute_pending_termination(task, settings)` в фоновом потоке.
3. Поток подключается к аккаунту, выполняет `terminate_specific_sessions`, помечает `done` или `failed`.

Если GUI был закрыт в момент `scheduled_at` — задача выполнится при следующем запуске задним числом. Цель — выглядеть естественно для Telegram («пользователь пришёл, навёл порядок»).

---

## 5. Раздел: Парсинг

UI: `ParsingFrame` (gui.py).

Назначение: собирать список пользователей из публичных групп для последующих рассылок.

### Кнопка «Спарсить группу»

Метод `_run_parse()`. Открывает диалог: ссылка на группу + опции:

- **Агрессивный режим** — если включён, попытается собрать всех участников через `GetParticipantsRequest` с разными фильтрами (подробности ниже).
- **Источник** — обычная парсинг (просто `client.iter_participants`) или агрессивный.

Стартует фоновый поток с `parser.GroupParser.parse_group()`.

### `GroupParser.parse_group()` (parser.py)

1. **Резолвит группу** через `client.get_entity(group)`. Если приватная и мы не вступили — `ChannelPrivateError`. В этом случае пытается `JoinChannelRequest`.

2. **Скачивает участников**:
    - Обычный режим: `client.iter_participants(group, aggressive=False)` — возвращает первые ~10000 участников.
    - Агрессивный режим: `client.iter_participants(group, aggressive=True)` — Telethon делает несколько запросов с разными фильтрами (recent, admins, kicked, banned), убирая дубликаты. Это позволяет обойти лимит 10000 в больших группах.

3. **Для каждого участника** проверяет:
    - Не бот (`bot=False`).
    - Не deleted (`deleted=False`).
    - Имеет username или хотя бы first_name (для упоминаний).

4. **Записывает в БД** через `db.add_parsed_user()` — таблица `parsed_users` (поля: `user_id`, `username`, `first_name`, `last_name`, `source_group`, `parsed_at`).

Возвращает количество добавленных пользователей.

### Кнопка «Парсинг по содержимому» (smart-parse)

Метод `_run_smart_parse()`. Парсит сообщения в группе/канале и пропускает их через AI-фильтр. Цель — собрать пользователей которые писали о конкретной теме.

Алгоритм:

1. **Загружает сообщения** через `client.iter_messages(group, limit=1000)`.
2. **Группирует пользователей** — для каждого собирает все его сообщения.
3. **AI-фильтр** через `ai_filter.filter_users_by_intent`:
    - Промпт: «Найди пользователей которые упоминали [тема]».
    - OpenAI/Groq возвращает список user_id'ов которые подошли.
4. **Записывает только подошедших** через `db.add_parsed_user()`.

Это медленно и тратит токены, но даёт намного более релевантную аудиторию.

### Кнопка «Парсинг комментаторов канала»

Метод `parser.parse_commenters()`. Для каналов с включёнными комментариями:

1. Получает последние N постов канала.
2. Для каждого поста — `iter_messages(post.discussion)` — собирает комментаторов.
3. Дедуплицирует и записывает в БД.

Полезно для целевых аудиторий — комментаторы тематического канала уже заинтересованы в теме.

### Кнопка «Удалить дубликаты»

Метод `db.dedupe_parsed_users()`. Удаляет записи где `user_id` встречается больше одного раза, оставляя самую свежую.

---

## 6. Раздел: Аудитории

UI: `AudiencesFrame` (gui.py).

Аудитория — это именованный список пользователей. Создаётся из `parsed_users` (отфильтрованных по разным критериям) и используется в DM-рассылках.

### Кнопка «Создать аудиторию»

Метод `_create_audience()`. Диалог:

- Имя аудитории.
- Источник — все парсенные пользователи / из конкретной группы / по AI-фильтру.
- Фильтры — последний онлайн (X дней), наличие username, первая буква имени.

Сохраняется в таблицу `audiences` (имя + критерии) и `audience_members` (привязка user_id к audience_id).

### Кнопка «Удалить аудиторию»

Метод `db.delete_audience(audience_id)`. Каскадно удаляет членов из `audience_members`.

---

## 7. Раздел: Рассылка

UI: `BroadcastFrame` (gui.py).

Объединяет 3 типа рассылок: упоминания в группах, прямые DM, broadcast в группах. Также — кнопка проверки групп.

### Общие настройки рассылки

В верхней части Frame:

- **Группы** — список через перевод строки.
- **Текст сообщения** — поддерживает spintax `{вариант1|вариант2}`.
- **Уникализация** — выпадающий: «нет» / «маска» / «AI».
- **Источник текста** — «введённый» / «избранное» (из Saved Messages аккаунта).

### Кнопка «DM-рассылка»

Метод `_run_dm()`. Запускает фоновый поток который:

1. **Загружает аудиторию** (выбранную в выпадающем списке).
2. **Загружает все активные аккаунты** через `db.get_active_accounts()` (проходят все проверки статус-машины).
3. **Распределяет членов аудитории** между аккаунтами равными порциями.
4. **Для каждого аккаунта** в отдельном потоке:
    - Подключается через `TelegramSender.connect()`.
    - Если не подключился — пропускает.
    - **Цикл по получателям**:
        - Spintax → уникализация → отправка через `sender.send_dm(user_id, username, message)`.
        - Возвращаемый статус: `sent` / `private` (юзер не принимает DM) / `error` / `flood_wait` / `banned`.
        - **Терминальные статусы** (`flood_wait`, `banned`) → break без паузы.
        - **Прочие** (`sent`, `private`, `error`) → пауза `random_dm_delay_sec(settings)` с диапазоном из настроек.
    - В лог идёт строка `Пауза 92с (диапазон 60-180с)...`.
5. **Логирует все попытки** через `db.log_send()` — таблица `send_log`.

### Кнопка «Упоминания»

Метод `_run_mention()`. Сложнее DM — упоминания идут в группах с тегом-меткой нескольких пользователей в одном сообщении.

Алгоритм:

1. **`mentioner.Mentioner.build_mention_message()`** — объединяет шаблон + список пользователей в текст с inline-mention'ами:
    - Шаблон: `Привет, {mentions}!`
    - Список: `[user1, user2, user3]`
    - Результат: текст + список `MessageEntityMentionName` для inline-упоминаний по user_id.
2. **Бьёт аудиторию на батчи** по `mentions_per_message` (default 2).
3. **Цикл по группам** + цикл по аккаунтам внутри + цикл по батчам:
    - Для каждой группы используем разные аккаунты, ротируем.
    - Для каждого батча: `sender.send_mention_message(group, text, entities, ...)`.
    - При `flood_wait` — ротация (берём следующий аккаунт).
    - При `no_permission`/`private` — группа недоступна, пропускаем все её батчи.
    - Между успешными отправками — пауза `random_mention_delay_sec(settings)`.
    - **При ошибках** (партия 4) — тоже пауза, но с пометкой «после ошибки».

### Кнопка «Broadcast»

Метод `_run_broadcast()`. Самый простой тип — отправка одного сообщения в каждую группу из списка.

Алгоритм:

1. **Создаются Task'и** в БД через `db.add_task()` — по одной на пару (account, group).
2. **Цикл по аккаунтам в потоках**:
    - Подключение.
    - Цикл по своим Task'ам:
        - Источник текста: «избранное» (через `sender.get_saved_message()`) или из таска.
        - Уникализация → `sender.send_broadcast_message(group, msg)`.
        - При `sent` → `db.mark_task_completed(task.id)`.
        - При `flood_wait`/`banned` → break.
        - Между попытками — пауза `random_broadcast_delay_sec(settings)`.

### Кнопка «Проверить группы»

Метод `_run_check_groups()`. Перед массовой рассылкой полезно проверить — мы вообще состоим в этих группах?

Алгоритм:

1. **Для каждой группы из списка**:
    - Берём первый активный аккаунт.
    - Пытаемся `client.get_entity(group)`. Если приватная — `ChannelPrivateError`.
    - Если не состоим — вызываем `parser.join_group(client, group)`:
        - Пытается `JoinChannelRequest` (обычный публичный канал/группа).
        - Если invite-link — `ImportChatInviteRequest`.
        - Возвращает статус: `joined` / `already_member` / `flood_wait` / `private` / `banned` / `error`.
2. **Между группами** — пауза `random_group_check_delay_sec(settings)` с диапазоном из настроек.

Эта команда заполняет участие нашими аккаунтами во всех нужных группах перед рассылкой.

### `TelegramSender.send_dm()` (sender.py)

Подробности:

1. Резолвит юзера через `client.get_input_entity(user_id)`. Если у юзера есть username — тоже пробует через него.
2. **Условие отправки**: `client.send_message(user, message)`.
3. **Возможные исключения**:
    - `UserPrivacyRestrictedError` → `private` (юзер закрыл DM).
    - `PeerFloodError` / `FloodWaitError` → `flood_wait`, ставит `flood_until`.
    - `UserDeactivatedBanError` / `PhoneNumberBannedError` → `banned`, ставит статус.
    - `UserBannedInChannelError`, прочее → `error`.
4. При `sent` — увеличивает `Account.sent_today`.
5. Все попытки (включая ошибки) логируются в `send_log` таблицу.

### `TelegramSender.send_mention_message()` (sender.py)

Аналогично DM, но шлёт сообщение в группу с пред-собранными `MessageEntityMentionName` объектами. Обработка ошибок такая же. Статусы — `sent`, `flood_wait`, `banned`, `no_permission` (нет прав постить), `private` (приватная группа куда мы не вступили).

### `TelegramSender.send_broadcast_message()` (sender.py)

Простая версия — `client.send_message(group, message)` без entities. Обрабатывает те же ошибки.

### Кольцевая ротация аккаунтов

В DM-рассылке используется простое распределение N пользователей на M аккаунтов. В упоминаниях — при `flood_wait` берём следующий аккаунт. Полноценной кольцевой ротации с очередью пока нет — это в TODO.

---

## 8. Раздел: Каналы

UI: `ChannelCommenterFrame` (gui.py).

Назначение: автоматический комментинг постов на каналах для прокачки активности.

### Два режима

#### Режим 1 — комментирование старых постов

Метод `channel_commenter.comment_old_posts(client, channel, limit, comment_template, ai)`:

1. Получает последние N постов канала через `iter_messages`.
2. Для каждого поста:
    - Проверяет включены ли комментарии (`message.replies` существует).
    - Через `iter_messages(post.discussion)` смотрит, не комментировали ли мы уже.
    - Если нет — генерирует комментарий (template + AI uniqueness, опционально).
    - `client.send_message(post.discussion, comment, reply_to=post.id)`.

#### Режим 2 — слушатель новых постов

Класс `NewPostListener`:

1. При `start()` регистрирует через `client.add_event_handler` обработчик на `NewMessage(chats=channel)`.
2. Когда канал постит новое сообщение — handler срабатывает в фоне.
3. Через рандомную задержку (имитация чтения) комментирует.
4. При `stop()` — handler отменяется.

Слушатель работает только пока запущен GUI.

---

## 9. Раздел: Автоответчик

UI: `AutoReplyFrame` (gui.py).

Назначение: автоматический ответ на DM от пользователей.

### Класс `AutoReplyListener`

Алгоритм:

1. При `start()` — `client.add_event_handler` на `NewMessage(incoming=True, func=lambda e: e.is_private)`.
2. При получении DM:
    - Проверяет что юзер не наш и не бот.
    - Опционально — фильтр по AI: «это интересный лид?» через `ai_filter`.
    - Если прошёл — отправляет ответ-шаблон с задержкой (имитация набора).
    - Записывает в `autoreply_log`.

Шаблон поддерживает spintax. Задержка рандомная (по умолчанию 30-180с).

---

## 10. Раздел: Объявления

UI: `AdsFrame` (ads_gui.py).

Это полноценный планировщик рекламных постов. Полностью отдельная система от broadcast.

### Архитектура

- **Таблица `ads`** — тексты + медиа.
- **Таблица `groups_targets`** — куда постить, с per-group интервалами, рабочими часами и статусом.
- **`AdsScheduler`** — фоновый поток, тикает раз в минуту.
- **`SubscriptionManager`** — проверка обязательных подписок перед публикацией.
- **`publish_to_group()`** — атомарная функция публикации.

### Подраздел «Объявления»

CRUD текстов + медиа. Каждое объявление имеет `enabled` флаг.

### Подраздел «Группы»

CRUD групп. Поля:

- `link` — ссылка/username.
- `interval_minutes` / `interval_minutes_max` — диапазон рандомного интервала между публикациями.
- `hours_start` / `hours_end` — рабочие часы (0-23).
- `next_allowed_at` — когда можно следующий раз постить (вычисляется после публикации).
- `status` — `active` / `paused` / `error` / `pending_join`.
- `join_status` — `joined` / `not_joined` / `error`.
- `retry_after` — если временная ошибка, когда повторить.

### Подраздел «Обязательные подписки»

Таблица `required_subs` — список каналов на которые наши аккаунты должны быть подписаны перед публикацией. Используется для целей вроде «рекламируем партнёра, обязательно показываем подписку на его канал».

### `AdsScheduler` (ads_scheduler.py) — главный планировщик

Запускается через `start()` — стартует daemon-thread с asyncio loop. Каждые 60с тикает `_tick()`:

#### `_tick()` — что делает каждую минуту

1. **Загружает активные настройки** — `db.load_scheduler_settings()`.
2. **Проверяет глобальные лимиты** через `_can_publish_globally()`:
    - `pub_count_today < daily_publication_limit`.
    - `time_since_last_publication >= publication_interval_min_seconds`.
3. **Ротация аккаунтов**: берёт следующий активный аккаунт через `db.get_active_accounts()`. Если ни одного — возвращает.
4. **Подключает клиента** через `_ensure_connected_client()`. Если упал — пропускает тик.
5. **Получает список активных групп** — `db.get_active_groups()` (только status=`active`, не в pause).
6. **Для каждой группы** — `_can_publish_to_group(group)`:
    - **Условие 1**: `group.status != 'active'` → `False`.
    - **Условие 2**: текущий час между `hours_start` и `hours_end`. Если нет → `False`.
    - **Условие 3**: `group.next_allowed_at` пусто или в прошлом. Если в будущем → `False`.
    - **Условие 4**: `group.retry_after` пусто или в прошлом. Если в будущем → `False`.
7. **Проверка обязательных подписок**:
    - `SubscriptionManager.ensure_subscriptions(client, group_id)` — для текущей группы (или глобально):
        - Берёт `db.get_required_subs()`.
        - Для каждой подписки → `_ensure_single(client, sub)`:
            - `check_membership(client, channel_link)` — пытается `client.get_entity(channel_link)`, проверяет `participant`.
            - Если не подписаны — `join_channel(client, channel_link)` через `JoinChannelRequest`.
            - Записывает в БД `is_joined=1`, `last_checked=now`.
        - Между join'ами — рандомная пауза `_compute_next_join_delay_sec()`.
    - Если хоть одна подписка не удалась — публикация в эту группу пропускается. Возвращает `False`.

8. **Выбирает объявление**:
    - Из `db.get_active_ads()` — берёт случайное (или round-robin, опционально).
9. **`_publish_one(db, ad, group, account)`**:
    - Вызывает `publish_to_group(client, group, ad)` из `ads_publisher.py`.
    - Возвращает `PublicationResult`: `success` / `flood_wait` / `slow_mode` / `banned` / `private` / `error`.
10. **На основе результата**:
    - **`success`**: пишет `publication_log` со статусом `sent`. Обновляет `group.next_allowed_at = now + random_interval(group.interval_minutes, group.interval_minutes_max)`.
    - **`flood_wait`**: ставит аккаунт в `flood_until`. Группу не трогает (пробуем другим аккаунтом).
    - **`slow_mode`**: устанавливает `group.retry_after = now + N секунд` (Telegram сказал «жди»).
    - **`banned`**: помечает `group.status = 'error'` или аккаунт banned.
    - **`private`**: `group.join_status = 'not_joined'`, попытка вступить через `parser.join_group()` в следующий тик.
    - **`error`**: `group.status = 'error'` с `last_error`.
11. **Глобальная пауза** между публикациями — `random_publication_interval_sec(settings)`.

### `publish_to_group()` (ads_publisher.py)

Атомарная функция отправки одного объявления в одну группу. Внутри:

1. Резолвит группу через `client.get_entity(group.link)`.
2. Если объявление с медиа — `client.send_file(group, ad.image_path, caption=ad.body)`.
3. Иначе — `client.send_message(group, ad.body)`.
4. Парсит SlowMode-ошибку через `_parse_until_datetime(error_text)` — Telegram пишет «ограничены до 23.04.2026, 21:53».
5. Возвращает `PublicationResult` со статусом и доп. данными.

### `_random_interval_sec(min_sec, max_sec, hard_min)` (ads_scheduler.py)

Безопасный random:

1. `lo = max(min_sec, hard_min)` — нижняя граница не ниже hard_min (анти-PeerFlood защита).
2. `hi = max(max_sec, lo)` — верхняя не ниже нижней (если юзер ввёл max < min, нормализуем).
3. `return random.uniform(lo, hi)`.

Хелперы `random_dm_delay_sec`, `random_mention_delay_sec`, `random_broadcast_delay_sec`, `random_group_check_delay_sec` — обёртки над этой функцией с разными hard_min'ами для разных типов рассылок.

---

## 11. Раздел: Настройки

UI: `SettingsFrame` (gui.py).

Прокручиваемый Frame с тремя секциями.

### Секция 1: API-ключи (.env)

Поля:

- `OPENAI_API_KEY` — ключ для AI-уникализации.
- `OPENAI_MODEL` — модель (default `gpt-4o-mini`).
- `OPENAI_PROXY` — прокси для запросов к OpenAI (РФ часто блокирует напрямую).
- `GROQ_API_KEY` — альтернативный AI-провайдер.
- `GROQ_PROXY` — прокси для Groq.

Read-only:
- `DB_PATH` — путь к teleton.db.
- `SESSIONS_DIR` — папка с .session-файлами.

При сохранении — `_update_env_file()` записывает в `.env`.

### Секция 2: Импорт TData (БД)

Загружается из `SchedulerSettings` через `AdsDB`:

- `tdata_connect_timeout_seconds` (default 60) — таймаут на `client.connect()` в шаге 5.
- `tdata_get_me_timeout_seconds` (default 60) — таймаут на `client.get_me()` в шаге 6.
- `tdata_flood_max_wait_seconds` (default 300) — макс. время ожидания FloodWait перед сдачей.
- `tdata_flood_jitter_min_seconds` (default 1) / `_max` (default 3) — рандомный jitter после FloodWait перед ретраем.

### Секция 3: Управление устройствами (БД)

- `device_terminate_delay_min_seconds` (default 1) / `_max` (default 3) — рандомная пауза между ResetAuthorizationRequest при удалении сессий.
- `device_terminate_default_schedule_hours` (default 2) — дефолтное значение в поле «через N» в DevicesDialog.

При сохранении валидация:
- Все int >= 1.
- `flood_jitter_min <= flood_jitter_max`.
- `device_terminate_delay_min <= device_terminate_delay_max`.

Сохранение через `AdsDB.save_scheduler_settings(settings)`.

---

## 12. Spintax + AI-уникализация

### `spintax.spin_text(text)` (spintax.py)

Парсит конструкции `{вариант1|вариант2|вариант3}` рекурсивно (поддерживает вложенность).

Алгоритм:

1. Находит самые внутренние `{...}` через regex.
2. Разбивает по `|`, выбирает случайный вариант.
3. Подставляет, повторяет с теми что снаружи.
4. Возвращает финальный текст без `{}`.

Применяется ко всем сообщениям перед отправкой.

### `spintax.spin_unique(text, count)` (spintax.py)

Генерирует `count` уникальных вариантов через многократный `spin_text`. Если возможных комбинаций меньше count — возвращает что есть с повторениями.

### `spintax.apply_mask(text, mask_path)` (spintax.py)

Подменяет латинские буквы на похожие из других алфавитов. Файл `mask.txt`:

```
a:а,à,á,ą
e:е,è,é,ę
o:о,ò,ó
```

Это «уникализация» которая делает каждое сообщение чуть-чуть разным глазом для антиспам-фильтров Telegram (но при этом читается одинаково).

### `spintax.ai_rewrite(text, api_key, model)` (spintax.py)

Через OpenAI/Groq генерирует переписанную версию текста с тем же смыслом. Используется в DM/broadcast/mention для уникализации.

Промпт что-то вроде:
```
Перепиши это сообщение, сохранив смысл, но изменив формулировки.
Не добавляй ничего нового, не убирай ничего важного.
Текст: {text}
```

### `_apply_unique(raw_msg, mode)` в gui.py

Объединяющая функция:
- `mode = "нет"` → возвращает как есть.
- `mode = "маска"` → `apply_mask(raw_msg)`.
- `mode = "AI"` → `ai_rewrite(raw_msg, api_key, model)`.

Вызывается в каждой рассылке перед отправкой.

---

## Важные системные особенности

### WAL и блокировки

Обе БД (`teleton.db` и тот же файл с ads-таблицами) работают в WAL-режиме. Параллельный доступ — нормально. Запись через `INSERT OR REPLACE` идемпотентна.

### Логирование

`file_logger.log_to_file(tag, message)` пишет в файл `data/logs/teleton_YYYY-MM-DD.log` с автоматическим открытием/закрытием. `log_exception(tag, e, context)` — пишет полный traceback (важно при `OpenTeleException` где `str(e)` ломается на Python 3.13).

### Threading

Все длительные операции — в фоновых потоках. Главный thread (Tk main loop) не блокируется. Связь между ними — через `log_queue` с тегами.

### Безопасность Telegram

- Все рандомные паузы используют `random.uniform` с настраиваемыми min/max.
- `hard_min` защита от случайного `min=0`.
- Между батчами в упоминаниях — паузы.
- При FloodWait — ретрай только если ждать недолго.
- Между удалениями сессий — настраиваемая пауза.

### Gracefulshutdown

При закрытии GUI:
- `AdsScheduler.stop()` останавливает фоновый thread.
- Все `Sender.disconnect()` вызываются для активных клиентов.
- Tk-таймеры отменяются автоматически с закрытием root window.

---

Это полное описание основной функциональности. Подразделы которые не так детально расписаны (Задачи, Аккаунт, Статистика) — это вспомогательные UI без сложной логики, в основном CRUD-операции над таблицами БД.
