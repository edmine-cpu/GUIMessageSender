# AGENTS.md

Quick map for future Codex passes. Keep this file short and update it when the project shape changes.

## Project Root

- Main code lives in the `TELETON_*` code folder.
- `tdata/` at workspace root contains Telegram Desktop account data. Treat it as sensitive runtime data. Do not inspect, copy, edit, delete, or move it unless the user explicitly asks.
- The code folder contains many duplicated Russian "copy" files, `backup_before_merge_*`, copied `data/` and `tests/` folders, and Grok handoff notes. Prefer the non-copy files and the live `tests/` directory unless the user asks about backups.

## Stack

- Python 3.12 Windows desktop app.
- GUI: `customtkinter` in `gui.py`, plus ads UI in `ads_gui.py`.
- Telegram: `telethon`, `opentele` for TData import, `python-socks`/`httpx[socks]` for proxies.
- AI providers: OpenAI and Groq.
- Database: SQLite at `data/teleton.db`, with WAL enabled in both `database.py` and `ads_database.py`.
- Tests: `pytest`, `pytest.ini` has `asyncio_mode = auto`.

## Common Commands

Run from the `TELETON_*` code folder.

- Install deps: `py -3.12 -m pip install -r requirements.txt`
- Project installer: `install_deps.bat`
- Start GUI: `run_gui.bat` or `py -3.12 gui.py`
- CLI help: `py -3.12 main.py --help`
- Full tests: `py -3.12 -m pytest tests/ -v`
- Focused tests: `py -3.12 -m pytest tests/test_spintax.py -v`

Do not run Telegram-live actions, TData import, message sends, session deletion, or cleanup scripts without explicit user approval.

## Entry Points

- `gui.py`: main desktop app. Large monolith. Key classes:
  - `TeletonApp`
  - `AccountsFrame`, `TasksFrame`, `ListTemplatesFrame`
  - `ParsingFrame`, `AudiencesFrame`, `BroadcastFrame`
  - `ChannelCommenterFrame`, `AutoReplyFrame`, `AccountManagementFrame`
  - `StatsFrame`, `SettingsFrame`
  - `ImportTDataDialog`, `DevicesDialog`, cycle dialogs
- `main.py`: CLI commands:
  - `parse`, `smart-parse`, `mention`, `broadcast`, `add-task`, `stats`
- `config.py`: `.env` loading and paths. Important env keys:
  - `DB_PATH`, `SESSIONS_DIR`
  - `OWN_API_ID`, `OWN_API_HASH`
  - `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_PROXY`
  - `GROQ_API_KEY`, `GROQ_PROXY`

## Core Modules

- `models.py`: dataclasses `Account`, `ParsedUser`, `Task`, `SendLog`, `MatchedPost`; account statuses are `active`, `needs_reauth`, `banned`, `network_issue`.
- `database.py`: main SQLite schema and CRUD. Important areas:
  - account status machine: `get_active_accounts`, `on_connect_success`, `on_connect_network_issue`, `on_connect_error`, `set_account_flood_until`
  - tasks and send logs: `get_pending_tasks`, `mark_task_completed`, `mark_task_waiting`, `mark_task_error`, `log_send`, `log_mention`
  - audiences: parsed users and matched posts
  - cycle campaigns: `cycle_campaigns`, `cycle_targets`, `cycle_state`, `cycle_campaign_accounts`
  - account action limiter: `try_acquire_action_slot`, `log_account_action`, `get_accounts_health`
- `sender.py`: `TelegramSender`, proxy parsing/normalization, Telethon client creation, connect status handling, send mention/DM/broadcast helpers.
- `parser.py`: `GroupParser`, group/commenter/content parsing, `join_group`, `inspect_chat_access`, `ensure_chat_access`.
- `mentioner.py`: mention entity builder.
- `spintax.py`: `spin_text`, `spin_unique`, `apply_mask`, `ai_rewrite`.
- `ai_filter.py`: OpenAI/Groq filtering for smart parsing.
- `file_logger.py`: file logs under `data/logs/teleton_YYYY-MM-DD.log` plus event logs.

## Ads Subsystem

- `ads_models.py`: `Ad`, `GroupTarget`, `Adaptation`, `PublicationLog`, `RequiredSub`, `SchedulerSettings`.
- `ads_database.py`: `AdsDB` and ads tables:
  - `ads`, `groups_targets`, `ads_adaptations`, `ads_groups`
  - `publications_log`, `required_subs`, `scheduler_settings`
  - `pending_device_terminations`
- `ads_scheduler.py`: `AdsScheduler`, random delay helpers, publish loop, hard minimum delay clamps.
- `ads_publisher.py`: atomic `publish_to_group` and `PublicationResult`.
- `ads_subscriptions.py`: required subscription checks/joins.
- `ads_ai.py`: ad generation/adaptation via OpenAI or Groq.
- `ads_gui.py`: ads UI tabs:
  - `GroupsTab`, `AdsTab`, `SchedulerTab`, `HistoryTab`, `QuickLaunchTab`, `AdsMainFrame`

## Account And Session Tools

- `account_manager.py`: profile updates, leaving groups/channels, deleting dialogs/bots, listing sessions, terminating sessions.
- `channel_commenter.py`: old-post comments and `NewPostListener`.
- `autoreply.py`: `AutoReplyListener`.
- `channel_ai.py`: AI comment generation.

Session safety notes:

- TData import uses Telegram Desktop credentials from `config.py`: `DESKTOP_API_ID = 2040`.
- Existing imported accounts may store per-account `api_id`, `api_hash`, and device fingerprint fields.
- Do not change API/fingerprint logic casually; tests cover `sender._create_client`.
- Device termination logic uses `pending_device_terminations` in `AdsDB` and GUI timers.

## Tests By Area

- DB migrations/account statuses: `tests/test_migration.py`, `tests/test_account_status.py`
- Sender API and proxies: `tests/test_sender_api_selection.py`, `tests/test_proxy_normalization.py`
- Spintax/masks: `tests/test_spintax.py`
- Ads DB/scheduler/random delays: `tests/test_ads_migration.py`, `tests/test_ads_stage2.py`, `tests/test_ads_random_intervals.py`, `tests/test_ads_helpers.py`
- Ads group interval reset: `tests/test_update_group_resets_next_allowed.py`
- Pending device termination: `tests/test_pending_device_terminations.py`
- TData import hints and flood retry: `tests/test_tdata_error_hints.py`, `tests/test_flood_retry.py`
- Cycle broadcast regressions: `tests/test_cycle_start_regression.py`, `tests/test_cycle_campaigns.py`, `tests/test_cycle_campaign_accounts.py`, `tests/test_cycle_persistence.py`
- Parser/input behavior: `tests/test_parse_links_input.py`, `tests/test_parser_stop.py`, `tests/test_parse_until_datetime.py`, `tests/test_matched_posts_audience.py`
- Dry-run side effect guards: `tests/test_dry_run_guards.py`
- Autoreply/channel log: `tests/test_autoreply_persistence.py`, `tests/test_channel_comment_log.py`

## Useful Search Shortcuts

- Classes in GUI: `rg -n "^class .*Frame|^class .*Dialog|^class .*App|^class" gui.py ads_gui.py`
- DB schema: `rg -n "CREATE TABLE|ALTER TABLE|PRAGMA user_version|CREATE INDEX" database.py ads_database.py`
- Public methods in a module: `rg -n "^class |^def |^async def |^    def |^    async def " sender.py parser.py`
- Tests for a symbol: `rg -n "symbol_name" tests`

## Editing Guidance

- Commit every completed change to Git after verification unless the user explicitly says not to commit.

- Prefer live files over Russian "copy" files.
- Keep changes scoped; `gui.py` is large and regression-prone.
- For GUI background work, preserve the existing queue/thread pattern: background threads put tagged messages into `log_queue`, Tk main thread consumes them.
- For database changes, keep migrations idempotent and add/adjust tests.
- For Telegram actions, preserve dry-run behavior and avoid network side effects in tests.
- After code changes, run the smallest relevant test set first, then broader tests if the touched area is shared.


