import sqlite3
import os
from datetime import datetime, date, timedelta
from typing import List, Set

from models import (
    Account, ParsedUser, Task, SendLog, MatchedPost,
    ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_NEEDS_REAUTH,
    ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NETWORK_ISSUE,
)


SCHEMA_VERSION = 18

# Cooldown для network_issue — через сколько пробуем reconnect
NETWORK_RECOVERY_MINUTES = 5
# Порог подряд неудачных connect'ов для автопометки
CONNECT_FAIL_THRESHOLD = 3


class Database:
    def __init__(self, db_path: str = "data/teleton.db"):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # WAL снижает contention между Database и AdsDB (пишут в один файл)
        # и делает crash-safe durability.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.init_db()

    def init_db(self):
        """Создание/миграция схемы через PRAGMA user_version."""
        cur = self.conn.cursor()
        current_version = cur.execute("PRAGMA user_version").fetchone()[0]

        # ─── v1: initial schema ──────────────────────────────────────────
        if current_version < 1:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    phone TEXT PRIMARY KEY,
                    session_name TEXT NOT NULL,
                    proxy TEXT DEFAULT '',
                    is_active INTEGER DEFAULT 1,
                    sent_today INTEGER DEFAULT 0,
                    last_reset_date TEXT DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS parsed_users (
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    phone TEXT,
                    access_hash INTEGER NOT NULL DEFAULT 0,
                    group_source TEXT NOT NULL,
                    status TEXT DEFAULT '',
                    is_bot INTEGER DEFAULT 0,
                    PRIMARY KEY (user_id, group_source)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_group TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    task_type TEXT DEFAULT 'broadcast',
                    source_group TEXT DEFAULT '',
                    mentions_per_message INTEGER DEFAULT 5,
                    completed INTEGER DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS matched_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER NOT NULL,
                    group_source TEXT NOT NULL,
                    sender_id INTEGER NOT NULL,
                    sender_username TEXT DEFAULT '',
                    sender_access_hash INTEGER NOT NULL DEFAULT 0,
                    message_text TEXT DEFAULT '',
                    match_mode TEXT DEFAULT '',
                    matched_keywords TEXT DEFAULT '',
                    ai_reason TEXT DEFAULT '',
                    matched_at TEXT NOT NULL,
                    UNIQUE(message_id, group_source)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_phone TEXT NOT NULL,
                    target_group TEXT NOT NULL,
                    message_text TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    error_detail TEXT DEFAULT '',
                    timestamp TEXT NOT NULL
                )
            """)

        # ─── v2: api_id / device fingerprint per account ─────────────────
        if current_version < 2:
            v2_columns = (
                "api_id INTEGER NOT NULL DEFAULT 0",
                "api_hash TEXT NOT NULL DEFAULT ''",
                "device_model TEXT DEFAULT ''",
                "system_version TEXT DEFAULT ''",
                "app_version TEXT DEFAULT ''",
                "lang_code TEXT DEFAULT 'en'",
            )
            for col_def in v2_columns:
                self._add_column_if_missing(cur, "accounts", col_def)

        # ─── v3: account status / cooldown / fail-counter ────────────────
        if current_version < 3:
            v3_columns = (
                "status TEXT NOT NULL DEFAULT 'active'",
                "flood_until TEXT DEFAULT ''",
                "connect_fail_count INTEGER DEFAULT 0",
                "last_status_change TEXT DEFAULT ''",
            )
            for col_def in v3_columns:
                self._add_column_if_missing(cur, "accounts", col_def)

            # Индексы для ускорения горячих запросов
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_send_log_target_status
                ON send_log(target_group, status)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_send_log_timestamp
                ON send_log(timestamp)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_parsed_users_group
                ON parsed_users(group_source, is_bot)
            """)

        # ─── v4: task status / retry-after / fail-counter ────────────────
        if current_version < 4:
            v4_task_columns = (
                "status TEXT NOT NULL DEFAULT 'pending'",
                "retry_after TEXT DEFAULT ''",
                "last_error TEXT DEFAULT ''",
                "fail_count INTEGER DEFAULT 0",
            )
            for col_def in v4_task_columns:
                self._add_column_if_missing(cur, "tasks", col_def)

        # ─── v5: list templates (groups/channels lists) ──────────────────
        if current_version < 5:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS list_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL DEFAULT 'mixed',
                    content TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

        # ─── v6: cyclic broadcast campaigns/targets/state ────────────────
        if current_version < 6:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    targets_source TEXT NOT NULL DEFAULT 'template',
                    template_id INTEGER DEFAULT NULL,
                    message_source TEXT NOT NULL DEFAULT 'manual',
                    message_text TEXT NOT NULL DEFAULT '',
                    unique_mode TEXT NOT NULL DEFAULT 'Оригинал',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_targets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id INTEGER NOT NULL,
                    pos INTEGER NOT NULL DEFAULT 0,
                    link TEXT NOT NULL,
                    hours_start INTEGER NOT NULL DEFAULT 0,
                    hours_end INTEGER NOT NULL DEFAULT 23,
                    min_interval_minutes INTEGER NOT NULL DEFAULT 0,
                    min_new_messages INTEGER NOT NULL DEFAULT 0,
                    fallback_hours INTEGER NOT NULL DEFAULT 0,
                    last_sent_at TEXT DEFAULT '',
                    last_seen_msg_id INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    retry_after TEXT DEFAULT '',
                    last_error TEXT DEFAULT '',
                    last_account_phone TEXT DEFAULT '',
                    last_text_preview TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    UNIQUE(campaign_id, link)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_state (
                    campaign_id INTEGER PRIMARY KEY,
                    current_pos INTEGER NOT NULL DEFAULT 0,
                    last_target_link TEXT DEFAULT '',
                    last_run_at TEXT DEFAULT '',
                    last_account_phone TEXT DEFAULT '',
                    last_text_preview TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cycle_targets_campaign_pos
                ON cycle_targets(campaign_id, pos)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_cycle_targets_campaign_retry
                ON cycle_targets(campaign_id, retry_after, status)
            """)

        # ─── v7: fallback-by-time for N-messages rule ────────────────────
        if current_version < 7:
            self._add_column_if_missing(
                cur, "cycle_targets", "fallback_hours INTEGER NOT NULL DEFAULT 0"
            )

        # ─── v8: proxy pool (для массового назначения прокси аккаунтам) ──
        if current_version < 8:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS proxy_pool (
                    proxy TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

        # ─── v9: cycle_targets интервалы в секундах (min/max) ────────────
        if current_version < 9:
            self._add_column_if_missing(
                cur, "cycle_targets", "interval_min_seconds INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                cur, "cycle_targets", "interval_max_seconds INTEGER NOT NULL DEFAULT 0"
            )

        # ─── v10: cycle_campaigns задержки цикла (send min/max + pause) ──
        if current_version < 10:
            self._add_column_if_missing(
                cur, "cycle_campaigns", "send_delay_min_seconds INTEGER NOT NULL DEFAULT 30"
            )
            self._add_column_if_missing(
                cur, "cycle_campaigns", "send_delay_max_seconds INTEGER NOT NULL DEFAULT 90"
            )
            self._add_column_if_missing(
                cur, "cycle_campaigns", "round_pause_seconds INTEGER NOT NULL DEFAULT 0"
            )

        # ─── v11: campaigns (accounts + rotation rule + stats) ───────────
        if current_version < 11:
            self._add_column_if_missing(
                cur, "cycle_campaigns", "account_filter TEXT NOT NULL DEFAULT ''"
            )
            self._add_column_if_missing(
                cur, "cycle_campaigns", "rotate_after_n_sends INTEGER NOT NULL DEFAULT 0"
            )

            self._add_column_if_missing(
                cur, "cycle_state", "sent_total INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                cur, "cycle_state", "error_total INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                cur, "cycle_state", "last_error TEXT NOT NULL DEFAULT ''"
            )
            self._add_column_if_missing(
                cur, "cycle_state", "last_account_send_count INTEGER NOT NULL DEFAULT 0"
            )

            cur.execute("""
                CREATE TABLE IF NOT EXISTS cycle_campaign_accounts (
                    campaign_id INTEGER NOT NULL,
                    account_phone TEXT NOT NULL,
                    pos INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (campaign_id, account_phone)
                )
            """)

        # ─── v12: matched_posts meta (origin group + message date/link) ──
        if current_version < 12:
            self._add_column_if_missing(
                cur, "matched_posts", "origin_group TEXT NOT NULL DEFAULT ''"
            )
            self._add_column_if_missing(
                cur, "matched_posts", "message_date TEXT NOT NULL DEFAULT ''"
            )
            self._add_column_if_missing(
                cur, "matched_posts", "message_link TEXT NOT NULL DEFAULT ''"
            )

        # ─── v13: channel commenting logs ────────────────────────────────
        if current_version < 13:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_comment_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    post_id INTEGER NOT NULL,
                    comment_id INTEGER NOT NULL DEFAULT 0,
                    account_phone TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_channel_comment_log_channel_post
                ON channel_comment_log(channel, post_id)
            """)

        # ─── v14: auto-reply history + replied registry ─────────────────
        if current_version < 14:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autoreply_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_phone TEXT NOT NULL,
                    peer_id INTEGER NOT NULL,
                    peer_username TEXT NOT NULL DEFAULT '',
                    peer_name TEXT NOT NULL DEFAULT '',
                    incoming_text TEXT NOT NULL DEFAULT '',
                    reply_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_autoreply_log_account_time
                ON autoreply_log(account_phone, created_at)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS autoreply_replied (
                    account_phone TEXT NOT NULL,
                    peer_id INTEGER NOT NULL,
                    replied_at TEXT NOT NULL,
                    PRIMARY KEY (account_phone, peer_id)
                )
            """)

        # ─── v15: account health + global limiter log ───────────────────
        if current_version < 15:
            v15_columns = (
                "paused_until TEXT NOT NULL DEFAULT ''",
                "pause_reason TEXT NOT NULL DEFAULT ''",
                "last_check_ok_at TEXT NOT NULL DEFAULT ''",
                "last_send_at TEXT NOT NULL DEFAULT ''",
                "last_action_at TEXT NOT NULL DEFAULT ''",
                "actions_today INTEGER NOT NULL DEFAULT 0",
                "error_today INTEGER NOT NULL DEFAULT 0",
                "last_error_at TEXT NOT NULL DEFAULT ''",
                "last_error_text TEXT NOT NULL DEFAULT ''",
            )
            for col_def in v15_columns:
                self._add_column_if_missing(cur, "accounts", col_def)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS account_action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_phone TEXT NOT NULL,
                    action_kind TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_account_action_log_phone_time
                ON account_action_log(account_phone, created_at)
            """)

        # ─── v16: access_hash for users (DM/mentions entity resolution) ──
        if current_version < 16:
            self._add_column_if_missing(
                cur, "parsed_users", "access_hash INTEGER NOT NULL DEFAULT 0"
            )
            self._add_column_if_missing(
                cur, "matched_posts", "sender_access_hash INTEGER NOT NULL DEFAULT 0"
            )

        # ─── v17: custom_name / метка для аккаунтов (чтобы различать номера) ─
        if current_version < 17:
            self._add_column_if_missing(
                cur, "accounts", "custom_name TEXT DEFAULT ''"
            )

        # ─── v18: campaign-local text template and default target rules ──
        if current_version < 18:
            v18_campaign_columns = (
                "message_template_id INTEGER DEFAULT NULL",
                "default_hours_start INTEGER NOT NULL DEFAULT 0",
                "default_hours_end INTEGER NOT NULL DEFAULT 23",
                "default_interval_min_seconds INTEGER NOT NULL DEFAULT 0",
                "default_interval_max_seconds INTEGER NOT NULL DEFAULT 0",
                "default_min_new_messages INTEGER NOT NULL DEFAULT 0",
                "default_fallback_hours INTEGER NOT NULL DEFAULT 0",
            )
            for col_def in v18_campaign_columns:
                self._add_column_if_missing(cur, "cycle_campaigns", col_def)

        cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    @staticmethod
    def _add_column_if_missing(cur: sqlite3.Cursor, table: str, col_def: str):
        """ALTER TABLE ADD COLUMN с идемпотентной обработкой дубликата."""
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    # --- Accounts ---

    def add_account(self, account: Account):
        """Добавить или обновить аккаунт (включая api/device/status поля)."""
        self.conn.execute("""
            INSERT OR REPLACE INTO accounts
            (phone, session_name, proxy, is_active, sent_today, last_reset_date,
             api_id, api_hash, device_model, system_version, app_version, lang_code,
             status, flood_until, connect_fail_count, last_status_change, custom_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            account.phone, account.session_name, account.proxy,
            int(account.is_active), account.sent_today, account.last_reset_date,
            account.api_id, account.api_hash,
            account.device_model, account.system_version,
            account.app_version, account.lang_code,
            account.status, account.flood_until,
            account.connect_fail_count, account.last_status_change,
            account.custom_name or "",
        ))
        self.conn.commit()

    @staticmethod
    def _row_to_account(row) -> Account:
        """Маппинг sqlite3.Row → Account с defensive-доступом к новым полям."""
        keys = row.keys()
        return Account(
            phone=row["phone"],
            session_name=row["session_name"],
            proxy=row["proxy"] or "",
            is_active=bool(row["is_active"]),
            sent_today=row["sent_today"],
            last_reset_date=row["last_reset_date"] or "",
            api_id=row["api_id"] if "api_id" in keys else 0,
            api_hash=row["api_hash"] if "api_hash" in keys else "",
            device_model=row["device_model"] if "device_model" in keys else "",
            system_version=row["system_version"] if "system_version" in keys else "",
            app_version=row["app_version"] if "app_version" in keys else "",
            lang_code=row["lang_code"] if "lang_code" in keys else "en",
            status=row["status"] if "status" in keys else ACCOUNT_STATUS_ACTIVE,
            flood_until=row["flood_until"] if "flood_until" in keys else "",
            connect_fail_count=row["connect_fail_count"] if "connect_fail_count" in keys else 0,
            last_status_change=row["last_status_change"] if "last_status_change" in keys else "",
            paused_until=row["paused_until"] if "paused_until" in keys else "",
            pause_reason=row["pause_reason"] if "pause_reason" in keys else "",
            last_check_ok_at=row["last_check_ok_at"] if "last_check_ok_at" in keys else "",
            last_send_at=row["last_send_at"] if "last_send_at" in keys else "",
            last_action_at=row["last_action_at"] if "last_action_at" in keys else "",
            actions_today=row["actions_today"] if "actions_today" in keys else 0,
            error_today=row["error_today"] if "error_today" in keys else 0,
            last_error_at=row["last_error_at"] if "last_error_at" in keys else "",
            last_error_text=row["last_error_text"] if "last_error_text" in keys else "",
            custom_name=row["custom_name"] if "custom_name" in keys else "",
        )

    def get_active_accounts(self) -> List[Account]:
        """Аккаунты, доступные для текущей ротации.
        Берём is_active=1, status in ('active', 'network_issue'), flood_until истёк.
        network_issue попадает в выборку когда cooldown закончился — чтобы sender
        мог попробовать reconnect. Если успех — on_connect_success вернёт в active.
        """
        today = date.today().isoformat()
        now_iso = datetime.now().isoformat()
        rows = self.conn.execute("""
            SELECT * FROM accounts
            WHERE is_active = 1
              AND status IN (?, ?)
              AND (flood_until = '' OR flood_until < ?)
              AND (paused_until = '' OR paused_until < ?)
        """, (ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_NETWORK_ISSUE, now_iso, now_iso)).fetchall()

        accounts = []
        for row in rows:
            acc = self._row_to_account(row)
            if acc.last_reset_date != today:
                acc.sent_today = 0
                acc.actions_today = 0
                acc.error_today = 0
                acc.last_reset_date = today
                self.conn.execute(
                    "UPDATE accounts SET sent_today = 0, actions_today = 0, error_today = 0, last_reset_date = ? WHERE phone = ?",
                    (today, acc.phone),
                )
                self.conn.commit()
            accounts.append(acc)
        return accounts

    def increment_sent_count(self, phone: str):
        """Увеличить счётчик отправленных сообщений."""
        self.conn.execute(
            "UPDATE accounts SET sent_today = sent_today + 1 WHERE phone = ?",
            (phone,),
        )
        self.conn.commit()

    def deactivate_account(self, phone: str):
        """Деактивация + статус banned. Legacy-совместимый метод."""
        now = datetime.now().isoformat()
        stamp = f"{now} | banned | manual or peerflood"
        self.conn.execute("""
            UPDATE accounts SET is_active=0, status=?, last_status_change=?
            WHERE phone=?
        """, (ACCOUNT_STATUS_BANNED, stamp, phone))
        self.conn.commit()

    def get_all_accounts(self) -> List[Account]:
        """Получить все аккаунты (для GUI)."""
        rows = self.conn.execute("SELECT * FROM accounts").fetchall()
        return [self._row_to_account(row) for row in rows]

    def delete_account(self, phone: str):
        """Удалить аккаунт из БД."""
        self.conn.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
        self.conn.commit()

    def activate_account(self, phone: str):
        """Ручная реактивация из GUI — сбрасывает весь auto-статус."""
        now = datetime.now().isoformat()
        stamp = f"{now} | active | manual reactivation"
        self.conn.execute("""
            UPDATE accounts SET is_active=1, status=?, connect_fail_count=0,
                                flood_until='', paused_until='', pause_reason='',
                                last_status_change=?
            WHERE phone=?
        """, (ACCOUNT_STATUS_ACTIVE, stamp, phone))
        self.conn.commit()

    def set_account_custom_name(self, phone: str, custom_name: str):
        """Установить/очистить кастомную метку аккаунта. Номер остаётся первичным ключом."""
        name = (custom_name or "").strip()
        self.conn.execute(
            "UPDATE accounts SET custom_name = ? WHERE phone = ?",
            (name, phone),
        )
        self.conn.commit()

    # --- Status machine (автопометка из sender'а) ---

    def set_account_pause(self, phone: str, paused_until_iso: str, reason: str = ""):
        self.conn.execute("""
            UPDATE accounts
            SET paused_until = ?, pause_reason = ?
            WHERE phone = ?
        """, ((paused_until_iso or "").strip(), (reason or "")[:200], phone))
        self.conn.commit()

    def clear_account_pause(self, phone: str):
        self.conn.execute("""
            UPDATE accounts
            SET paused_until = '', pause_reason = ''
            WHERE phone = ?
        """, (phone,))
        self.conn.commit()

    def log_account_action(
        self,
        account_phone: str,
        action_kind: str,
        target: str,
        status: str,
        error_text: str = "",
        created_at: str = "",
    ):
        today = date.today().isoformat()
        ts = (created_at or "").strip() or datetime.now().isoformat(timespec="microseconds")
        phone = (account_phone or "").strip()

        row = self.conn.execute(
            "SELECT last_reset_date FROM accounts WHERE phone = ?",
            (phone,),
        ).fetchone()
        last_reset = (row["last_reset_date"] if row else "") if row is not None else ""
        if last_reset != today:
            self.conn.execute(
                "UPDATE accounts SET sent_today=0, actions_today=0, error_today=0, last_reset_date=? WHERE phone=?",
                (today, phone),
            )

        self.conn.execute("""
            INSERT INTO account_action_log
            (account_phone, action_kind, target, status, error_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            phone,
            (action_kind or "").strip(),
            (target or "")[:200],
            (status or "").strip(),
            (error_text or "")[:300],
            ts,
        ))

        fields = ["last_action_at = ?", "actions_today = actions_today + 1"]
        params: list = [ts]
        if status == "sent":
            fields.append("last_send_at = ?")
            params.append(ts)
            fields.append("sent_today = sent_today + 1")
        # error_today counts any problematic attempt (including per-target rejections)
        if status in ("error", "flood_wait", "banned", "no_permission", "private", "need_subscription", "slow_mode", "chat_banned"):
            fields.append("error_today = error_today + 1")

        # last_error_text records the last notable problem for the account.
        # We still set it for "error" (generic) and the global signals so that detailed last error is available for debugging / other tools.
        # BUT the Accounts tab "Почему" (see gui.py) ONLY surfaces it when the text contains clear global-ban / flood / auth signatures.
        # Per-chat rejections like "chat_banned" (UserBannedInChannelError on a specific board) deliberately do not set last_error_text at all.
        if status in ("banned", "flood_wait", "error"):
            fields.append("last_error_at = ?")
            params.append(ts)
            fields.append("last_error_text = ?")
            params.append((error_text or status)[:200])

        params.append(phone)
        self.conn.execute(f"""
            UPDATE accounts
            SET {", ".join(fields)}
            WHERE phone = ?
        """, params)
        self.conn.commit()

    def try_acquire_action_slot(
        self,
        account_phone: str,
        action_kind: str,
        min_interval_seconds: float = 2.0,
        daily_actions_limit: int = 200,
    ) -> tuple[bool, str, float]:
        """
        Единый лимитер: атомарно проверяет, можно ли выполнить действие с аккаунта.
        Возвращает (ok, reason, wait_seconds).
        """
        phone = (account_phone or "").strip()
        action_kind = (action_kind or "").strip()
        now = datetime.now()
        now_iso = now.isoformat(timespec="microseconds")

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("SELECT * FROM accounts WHERE phone = ?", (phone,)).fetchone()
            if not row:
                self.conn.execute("ROLLBACK")
                return False, "not_found", 0.0

            acc = self._row_to_account(row)

            today = date.today().isoformat()
            if acc.last_reset_date != today:
                self.conn.execute(
                    "UPDATE accounts SET sent_today=0, actions_today=0, error_today=0, last_reset_date=? WHERE phone=?",
                    (today, phone),
                )
                acc.sent_today = 0
                acc.actions_today = 0
                acc.error_today = 0
                acc.last_reset_date = today

            if not acc.is_active:
                self.conn.execute("ROLLBACK")
                return False, "inactive", 0.0

            if acc.status in (ACCOUNT_STATUS_NEEDS_REAUTH, ACCOUNT_STATUS_BANNED):
                self.conn.execute("ROLLBACK")
                return False, acc.status, 0.0

            if acc.paused_until:
                try:
                    if datetime.fromisoformat(acc.paused_until) > now:
                        self.conn.execute("ROLLBACK")
                        return False, "paused", 0.0
                except Exception:
                    pass

            if acc.flood_until:
                try:
                    if datetime.fromisoformat(acc.flood_until) > now:
                        self.conn.execute("ROLLBACK")
                        return False, "flood_wait", 0.0
                except Exception:
                    pass

            if int(acc.actions_today or 0) >= int(daily_actions_limit or 0):
                until = datetime.combine(date.today() + timedelta(days=1), datetime.min.time()).isoformat(timespec="seconds")
                self.conn.execute(
                    "UPDATE accounts SET paused_until=?, pause_reason=? WHERE phone=?",
                    (until, f"daily_actions_limit:{daily_actions_limit}", phone),
                )
                self.conn.execute("COMMIT")
                return False, "daily_limit", 0.0

            if acc.last_action_at:
                try:
                    last = datetime.fromisoformat(acc.last_action_at)
                    elapsed = (now - last).total_seconds()
                    if elapsed < float(min_interval_seconds or 0):
                        wait_s = float(min_interval_seconds) - elapsed
                        self.conn.execute("ROLLBACK")
                        return False, "min_interval", max(0.0, wait_s)
                except Exception:
                    pass

            self.conn.execute("""
                UPDATE accounts
                SET last_action_at = ?
                WHERE phone = ?
            """, (now_iso, phone))
            self.conn.execute("COMMIT")
            return True, "ok", 0.0
        except Exception:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            return False, "db_error", 0.0

    def get_accounts_health(self) -> List[dict]:
        """Health-таблица по аккаунтам для GUI."""
        today = date.today().isoformat()
        now = datetime.now()
        now_iso = now.isoformat()
        rows = self.conn.execute("SELECT * FROM accounts ORDER BY phone").fetchall()

        result = []
        for row in rows:
            acc = self._row_to_account(row)
            if acc.last_reset_date != today:
                self.conn.execute(
                    "UPDATE accounts SET sent_today=0, actions_today=0, error_today=0, last_reset_date=? WHERE phone=?",
                    (today, acc.phone),
                )
                self.conn.commit()
                acc.sent_today = 0
                acc.actions_today = 0
                acc.error_today = 0
                acc.last_reset_date = today

            state = acc.status or "active"
            why = ""
            if not acc.is_active:
                state = "inactive"
                why = "выключен пользователем"
            else:
                if acc.paused_until:
                    try:
                        until = datetime.fromisoformat(acc.paused_until)
                        if until > now:
                            state = "paused"
                            why = acc.pause_reason or f"до {until.strftime('%H:%M')}"
                    except Exception:
                        pass
                if state not in ("paused",) and acc.flood_until:
                    try:
                        until = datetime.fromisoformat(acc.flood_until)
                        if until > now:
                            state = "flood_wait"
                            why = f"до {until.strftime('%H:%M')}"
                    except Exception:
                        pass

                if state not in ("paused", "flood_wait") and acc.status in (ACCOUNT_STATUS_NEEDS_REAUTH, ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NETWORK_ISSUE):
                    state = acc.status
                    if acc.status == ACCOUNT_STATUS_NEEDS_REAUTH:
                        why = "нужен переимпорт"
                    elif acc.status == ACCOUNT_STATUS_BANNED:
                        why = "бан Telegram"
                    elif acc.status == ACCOUNT_STATUS_NETWORK_ISSUE:
                        why = "сеть/прокси (cooldown)"

            # Sanitize misleading per-target "last_error_text" so that Accounts "Почему" only shows real global bans.
            # This cleans up historical noise (e.g. "UserBannedInChannelError" from boards where only some chats reject the account).
            # The detailed per-target errors stay in cycle_targets.last_error and account_action_log.
            last_err = (acc.last_error_text or "").strip()
            if last_err and not why:
                low = last_err.lower()
                chat_specific = any(sig in low for sig in (
                    "userbannedinchannel", "chat_banned", "banned in this chat", "юзер банед", "banned/запрещён в этом"
                ))
                if chat_specific:
                    # Clear it so "Почему" becomes clean "—" (or busy context). Safe because no global why was set.
                    try:
                        self.conn.execute("UPDATE accounts SET last_error_text='', last_error_at='' WHERE phone=?", (acc.phone,))
                        self.conn.commit()
                        acc.last_error_text = ""
                    except Exception:
                        pass

            result.append({
                "phone": acc.phone,
                "proxy": acc.proxy or "",
                "is_active": bool(acc.is_active),
                "health": state,
                "why": why,
                "last_check_ok_at": acc.last_check_ok_at,
                "last_send_at": acc.last_send_at,
                "sent_today": int(acc.sent_today or 0),
                "actions_today": int(acc.actions_today or 0),
                "error_today": int(acc.error_today or 0),
                "last_error_text": acc.last_error_text or "",
                "flood_until": acc.flood_until or "",
                "paused_until": acc.paused_until or "",
                "now": now_iso,
                "custom_name": getattr(acc, "custom_name", "") or "",
            })
        return result

    def set_account_status(self, phone: str, status: str, reason: str = ""):
        """Установить статус аккаунта с меткой времени и причиной."""
        now = datetime.now().isoformat()
        stamp = f"{now} | {status} | {reason[:200]}"
        self.conn.execute(
            "UPDATE accounts SET status=?, last_status_change=? WHERE phone=?",
            (status, stamp, phone),
        )
        self.conn.commit()

    def set_account_flood_until(self, phone: str, flood_until_iso: str):
        """Поставить аккаунт на паузу до flood_until_iso."""
        self.conn.execute(
            "UPDATE accounts SET flood_until=? WHERE phone=?",
            (flood_until_iso, phone),
        )
        self.conn.commit()

    def on_connect_success(self, phone: str):
        """Успешный connect — сбросить счётчик fail'ов.
        Если был network_issue — вернуть в active, плюс снять flood_until."""
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE accounts
            SET connect_fail_count=0,
                status=CASE WHEN status=? THEN ? ELSE status END,
                flood_until=CASE WHEN status=? THEN '' ELSE flood_until END,
                last_check_ok_at=?
            WHERE phone=?
        """, (ACCOUNT_STATUS_NETWORK_ISSUE, ACCOUNT_STATUS_ACTIVE,
              ACCOUNT_STATUS_NETWORK_ISSUE, now, phone))
        self.conn.commit()

    def on_connect_network_issue(self, phone: str, reason: str = ""):
        """Сетевая ошибка. При CONNECT_FAIL_THRESHOLD+ подряд — помечаем
        network_issue и ставим cooldown NETWORK_RECOVERY_MINUTES."""
        self.conn.execute(
            "UPDATE accounts SET connect_fail_count=connect_fail_count+1, last_error_at=?, last_error_text=? WHERE phone=?",
            (datetime.now().isoformat(timespec="seconds"), f"network:{reason[:200]}", phone),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT connect_fail_count FROM accounts WHERE phone=?", (phone,)
        ).fetchone()
        if row and row["connect_fail_count"] >= CONNECT_FAIL_THRESHOLD:
            now = datetime.now()
            cooldown_until = (now + timedelta(minutes=NETWORK_RECOVERY_MINUTES)).isoformat()
            stamp = f"{now.isoformat()} | {ACCOUNT_STATUS_NETWORK_ISSUE} | {reason[:200] or 'threshold reached'}"
            self.conn.execute("""
                UPDATE accounts
                SET status=?, flood_until=?, last_status_change=?
                WHERE phone=?
            """, (ACCOUNT_STATUS_NETWORK_ISSUE, cooldown_until, stamp, phone))
            self.conn.commit()

    def on_connect_error(self, phone: str, reason: str = ""):
        """Неопознанная ошибка. При CONNECT_FAIL_THRESHOLD+ — needs_reauth."""
        self.conn.execute(
            "UPDATE accounts SET connect_fail_count=connect_fail_count+1, last_error_at=?, last_error_text=? WHERE phone=?",
            (datetime.now().isoformat(timespec="seconds"), f"connect:{reason[:200]}", phone),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT connect_fail_count FROM accounts WHERE phone=?", (phone,)
        ).fetchone()
        if row and row["connect_fail_count"] >= CONNECT_FAIL_THRESHOLD:
            self.set_account_status(
                phone, ACCOUNT_STATUS_NEEDS_REAUTH,
                reason[:100] or "threshold reached",
            )

    # --- Tasks (GUI) ---

    def get_all_tasks(self) -> List[Task]:
        """Получить все задачи (для GUI)."""
        rows = self.conn.execute("SELECT * FROM tasks").fetchall()
        return [
            Task(
                id=r["id"],
                target_group=r["target_group"],
                message_text=r["message_text"],
                task_type=r["task_type"],
                source_group=r["source_group"] or "",
                mentions_per_message=r["mentions_per_message"],
                completed=bool(r["completed"]),
                status=r["status"] if "status" in r.keys() else "pending",
                retry_after=r["retry_after"] if "retry_after" in r.keys() else "",
                last_error=r["last_error"] if "last_error" in r.keys() else "",
                fail_count=r["fail_count"] if "fail_count" in r.keys() else 0,
            )
            for r in rows
        ]

    def delete_task(self, task_id: int):
        """Удалить задачу из БД."""
        self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        self.conn.commit()

    # --- ParsedUsers (GUI) ---

    def get_parsed_groups_stats(self) -> List[dict]:
        """Статистика по спарсенным группам (group_source → count)."""
        rows = self.conn.execute("""
            SELECT group_source, COUNT(*) as cnt
            FROM parsed_users
            GROUP BY group_source
            ORDER BY cnt DESC
        """).fetchall()
        return [{"group_source": r["group_source"], "count": r["cnt"]} for r in rows]

    def get_all_audiences(self) -> List[dict]:
        """Все аудитории (для раздела GUI)."""
        result = []

        rows = self.conn.execute("""
            SELECT group_source, COUNT(*) as cnt
            FROM parsed_users
            GROUP BY group_source
            ORDER BY cnt DESC
        """).fetchall()
        for r in rows:
            result.append({
                "group_source": r["group_source"],
                "audience_type": "users",
                "count": r["cnt"],
                "last_date": "",
            })

        rows = self.conn.execute("""
            SELECT
                CASE
                    WHEN origin_group IS NOT NULL AND origin_group != '' THEN origin_group
                    ELSE group_source
                END as audience_key,
                COUNT(DISTINCT sender_id) as cnt,
                MAX(matched_at) as last_date
            FROM matched_posts
            GROUP BY audience_key
            ORDER BY cnt DESC
        """).fetchall()
        for r in rows:
            result.append({
                "group_source": r["audience_key"],
                "audience_type": "matched",
                "count": r["cnt"],
                "last_date": r["last_date"] or "",
            })

        return result

    def get_audience_members(self, group_source: str, audience_type: str) -> List[dict]:
        """Участники аудитории для экспорта/DM."""
        if audience_type == "users":
            rows = self.conn.execute("""
                SELECT user_id, username, first_name, last_name, access_hash
                FROM parsed_users
                WHERE group_source = ? AND is_bot = 0
            """, (group_source,)).fetchall()
            return [
                {
                    "user_id": r["user_id"],
                    "username": r["username"] or "",
                    "first_name": r["first_name"] or "",
                    "last_name": r["last_name"] or "",
                    "access_hash": int(r["access_hash"] or 0) if "access_hash" in r.keys() else 0,
                }
                for r in rows
            ]
        elif audience_type == "matched":
            rows = self.conn.execute("""
                SELECT DISTINCT sender_id, sender_username, sender_access_hash
                FROM matched_posts
                WHERE (
                    (origin_group IS NOT NULL AND origin_group != '' AND origin_group = ?)
                    OR ((origin_group IS NULL OR origin_group = '') AND group_source = ?)
                )
            """, (group_source, group_source)).fetchall()
            return [
                {
                    "user_id": r["sender_id"],
                    "username": r["sender_username"] or "",
                    "first_name": "",
                    "last_name": "",
                    "access_hash": int(r["sender_access_hash"] or 0) if "sender_access_hash" in r.keys() else 0,
                }
                for r in rows
            ]
        else:
            return []

    def delete_audience(self, group_source: str, audience_type: str) -> int:
        """Удалить одну аудиторию из parsed_users или matched_posts."""
        group_source = (group_source or "").strip()
        audience_type = (audience_type or "").strip()
        if not group_source:
            return 0

        if audience_type == "users":
            cur = self.conn.execute(
                "DELETE FROM parsed_users WHERE group_source = ?",
                (group_source,),
            )
        elif audience_type == "matched":
            cur = self.conn.execute("""
                DELETE FROM matched_posts
                WHERE (
                    origin_group = ?
                    OR ((origin_group IS NULL OR origin_group = '') AND group_source = ?)
                )
            """, (group_source, group_source))
        else:
            return 0

        self.conn.commit()
        return int(cur.rowcount or 0)

    def get_matched_posts_context(self, group_source: str, limit: int | None = None) -> List[dict]:
        """Контекст найденных постов для ручной проверки аудитории и CSV."""
        sql = """
            SELECT *
            FROM matched_posts
            WHERE (
                (origin_group IS NOT NULL AND origin_group != '' AND origin_group = ?)
                OR ((origin_group IS NULL OR origin_group = '') AND group_source = ?)
            )
            ORDER BY id DESC
        """
        params: list = [group_source, group_source]
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "user_id": r["sender_id"],
                "username": r["sender_username"] or "",
                "source_chat": r["group_source"] or "",
                "message_date": r["message_date"] or "",
                "message_link": r["message_link"] or "",
                "message_text": r["message_text"] or "",
                "match_mode": r["match_mode"] or "",
                "matched_keywords": r["matched_keywords"] or "",
                "ai_reason": r["ai_reason"] or "",
                "matched_at": r["matched_at"] or "",
            }
            for r in rows
        ]

    # --- MatchedPosts ---

    def save_matched_post(self, post: MatchedPost):
        """Сохранить найденный пост (INSERT OR IGNORE)."""
        self.conn.execute("""
            INSERT OR IGNORE INTO matched_posts
            (message_id, group_source, origin_group, message_date, message_link,
             sender_id, sender_username, sender_access_hash,
             message_text, match_mode, matched_keywords, ai_reason, matched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            post.message_id,
            post.group_source,
            post.origin_group or "",
            post.message_date or "",
            post.message_link or "",
            post.sender_id,
            post.sender_username or "",
            int(getattr(post, "sender_access_hash", 0) or 0),
            post.message_text,
            post.match_mode, post.matched_keywords, post.ai_reason,
            post.matched_at,
        ))
        self.conn.commit()

    def get_matched_posts(self, group_source: str, limit: int | None = None, matched_since: str | None = None) -> List[MatchedPost]:
        """Получить найденные посты по аудитории (или по группе для legacy-данных)."""
        sql = """
            SELECT *
            FROM matched_posts
            WHERE (
                (origin_group IS NOT NULL AND origin_group != '' AND origin_group = ?)
                OR ((origin_group IS NULL OR origin_group = '') AND group_source = ?)
            )
        """
        params: list = [group_source, group_source]
        if matched_since:
            sql += " AND matched_at >= ?"
            params.append(matched_since)
        sql += " ORDER BY id DESC"
        if limit is not None and int(limit) > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [
            MatchedPost(
                id=r["id"],
                message_id=r["message_id"],
                group_source=r["group_source"],
                origin_group=r["origin_group"] if "origin_group" in r.keys() else "",
                message_date=r["message_date"] if "message_date" in r.keys() else "",
                message_link=r["message_link"] if "message_link" in r.keys() else "",
                sender_id=r["sender_id"],
                sender_username=r["sender_username"],
                sender_access_hash=int(r["sender_access_hash"] or 0) if "sender_access_hash" in r.keys() else 0,
                message_text=r["message_text"],
                match_mode=r["match_mode"],
                matched_keywords=r["matched_keywords"],
                ai_reason=r["ai_reason"],
                matched_at=r["matched_at"],
            )
            for r in rows
        ]

    def get_matched_posts_stats(self) -> List[dict]:
        """Статистика по matched_posts."""
        rows = self.conn.execute("""
            SELECT
                CASE
                    WHEN origin_group IS NOT NULL AND origin_group != '' THEN origin_group
                    ELSE group_source
                END as audience_key,
                COUNT(*) as cnt
            FROM matched_posts
            GROUP BY audience_key
            ORDER BY cnt DESC
        """).fetchall()
        return [{"group_source": r["audience_key"], "count": r["cnt"]} for r in rows]

    # --- ChannelCommentLog ---

    def log_channel_comment(
        self,
        channel: str,
        post_id: int,
        comment_id: int,
        account_phone: str,
        status: str,
        error_text: str = "",
        created_at: str = "",
    ):
        ts = (created_at or "").strip() or datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO channel_comment_log
            (channel, post_id, comment_id, account_phone, status, error_text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            (channel or "").strip(),
            int(post_id or 0),
            int(comment_id or 0),
            (account_phone or "").strip(),
            (status or "").strip(),
            (error_text or "")[:500],
            ts,
        ))
        self.conn.commit()

    def has_successful_channel_comment(
        self,
        channel: str,
        post_id: int,
        account_phone: str,
    ) -> bool:
        row = self.conn.execute("""
            SELECT 1
            FROM channel_comment_log
            WHERE channel = ?
              AND post_id = ?
              AND account_phone = ?
              AND status = 'sent'
            LIMIT 1
        """, (
            (channel or "").strip(),
            int(post_id or 0),
            (account_phone or "").strip(),
        )).fetchone()
        return row is not None

    # --- AutoReply ---

    def log_autoreply_event(
        self,
        account_phone: str,
        peer_id: int,
        peer_username: str,
        peer_name: str,
        incoming_text: str,
        reply_text: str,
        status: str,
        reason: str = "",
        created_at: str = "",
    ):
        ts = (created_at or "").strip() or datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO autoreply_log
            (account_phone, peer_id, peer_username, peer_name, incoming_text, reply_text, status, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            (account_phone or "").strip(),
            int(peer_id or 0),
            (peer_username or "").strip(),
            (peer_name or "").strip(),
            (incoming_text or "")[:800],
            (reply_text or "")[:800],
            (status or "").strip(),
            (reason or "")[:300],
            ts,
        ))
        self.conn.commit()

    def has_autoreplied_forever(self, account_phone: str, peer_id: int) -> bool:
        row = self.conn.execute("""
            SELECT 1
            FROM autoreply_replied
            WHERE account_phone = ? AND peer_id = ?
        """, ((account_phone or "").strip(), int(peer_id or 0))).fetchone()
        return bool(row)

    def mark_autoreplied_forever(self, account_phone: str, peer_id: int, replied_at: str = ""):
        ts = (replied_at or "").strip() or datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO autoreply_replied (account_phone, peer_id, replied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(account_phone, peer_id) DO UPDATE SET
                replied_at = excluded.replied_at
        """, ((account_phone or "").strip(), int(peer_id or 0), ts))
        self.conn.commit()

    # --- ParsedUsers ---

    def save_parsed_users(self, users: List[ParsedUser]):
        """Сохранить спарсенных пользователей (upsert)."""
        if not users:
            return
        self.conn.executemany("""
            INSERT INTO parsed_users
            (user_id, username, first_name, last_name, phone, access_hash, group_source, status, is_bot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, group_source) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                phone = excluded.phone,
                access_hash = CASE
                    WHEN excluded.access_hash IS NOT NULL AND excluded.access_hash != 0
                        THEN excluded.access_hash
                    ELSE parsed_users.access_hash
                END,
                status = excluded.status,
                is_bot = excluded.is_bot
        """, [
            (u.user_id, u.username, u.first_name, u.last_name,
             u.phone, int(getattr(u, "access_hash", 0) or 0),
             u.group_source, u.status, int(u.is_bot))
            for u in users
        ])
        self.conn.commit()

    def get_users_for_mention(self, source_group: str, exclude_ids: Set[int] = None,
                              limit: int = 0) -> List[ParsedUser]:
        """Получить пользователей для упоминания (исключая уже упомянутых)."""
        if exclude_ids is None:
            exclude_ids = set()

        query = """
            SELECT * FROM parsed_users
            WHERE group_source = ? AND is_bot = 0
        """
        params: list = [source_group]

        if exclude_ids:
            placeholders = ",".join("?" * len(exclude_ids))
            query += f" AND user_id NOT IN ({placeholders})"
            params.extend(exclude_ids)

        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        rows = self.conn.execute(query, params).fetchall()
        return [
            ParsedUser(
                user_id=r["user_id"],
                username=r["username"],
                first_name=r["first_name"],
                last_name=r["last_name"],
                phone=r["phone"],
                access_hash=int(r["access_hash"] or 0) if "access_hash" in r.keys() else 0,
                group_source=r["group_source"],
                status=r["status"],
                is_bot=bool(r["is_bot"]),
            )
            for r in rows
        ]

    # --- Tasks ---

    def get_pending_tasks(self, task_type: str = "") -> List[Task]:
        """Получить невыполненные задачи."""
        now_iso = datetime.now().isoformat()
        if task_type:
            rows = self.conn.execute(
                "SELECT * FROM tasks "
                "WHERE completed = 0 "
                "  AND task_type = ? "
                "  AND status IN ('pending','waiting') "
                "  AND (retry_after = '' OR retry_after < ?)",
                (task_type, now_iso),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM tasks "
                "WHERE completed = 0 "
                "  AND status IN ('pending','waiting') "
                "  AND (retry_after = '' OR retry_after < ?)",
                (now_iso,),
            ).fetchall()

        return [
            Task(
                id=r["id"],
                target_group=r["target_group"],
                message_text=r["message_text"],
                task_type=r["task_type"],
                source_group=r["source_group"] or "",
                mentions_per_message=r["mentions_per_message"],
                completed=bool(r["completed"]),
                status=r["status"] if "status" in r.keys() else "pending",
                retry_after=r["retry_after"] if "retry_after" in r.keys() else "",
                last_error=r["last_error"] if "last_error" in r.keys() else "",
                fail_count=r["fail_count"] if "fail_count" in r.keys() else 0,
            )
            for r in rows
        ]

    def mark_task_completed(self, task_id: int):
        """Отметить задачу как выполненную."""
        self.conn.execute(
            "UPDATE tasks SET completed = 1, status = 'done' WHERE id = ?",
            (task_id,),
        )
        self.conn.commit()

    def mark_task_waiting(self, task_id: int, retry_after: str,
                          last_error: str = ""):
        self.conn.execute("""
            UPDATE tasks
            SET status='waiting', retry_after=?, last_error=?,
                fail_count=fail_count+1
            WHERE id=?
        """, (retry_after, last_error[:500], task_id))
        self.conn.commit()

    def mark_task_error(self, task_id: int, last_error: str = ""):
        self.conn.execute("""
            UPDATE tasks
            SET status='error', last_error=?, fail_count=fail_count+1
            WHERE id=?
        """, (last_error[:500], task_id))
        self.conn.commit()

    def reset_task_to_pending(self, task_id: int):
        self.conn.execute("""
            UPDATE tasks
            SET status='pending', retry_after='', last_error='', fail_count=0
            WHERE id=?
        """, (task_id,))
        self.conn.commit()

    def add_task(self, task: Task):
        """Добавить задачу."""
        self.conn.execute("""
            INSERT INTO tasks (target_group, message_text, task_type, source_group, mentions_per_message)
            VALUES (?, ?, ?, ?, ?)
        """, (
            task.target_group, task.message_text, task.task_type,
            task.source_group, task.mentions_per_message,
        ))
        self.conn.commit()

    # --- ListTemplates ---

    def get_all_list_templates(self) -> List[dict]:
        rows = self.conn.execute("""
            SELECT id, name, kind, content, created_at, updated_at
            FROM list_templates
            ORDER BY updated_at DESC, name ASC
        """).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "kind": r["kind"],
                "content": r["content"] or "",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def get_list_template(self, template_id: int) -> dict | None:
        row = self.conn.execute("""
            SELECT id, name, kind, content, created_at, updated_at
            FROM list_templates
            WHERE id = ?
        """, (template_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "content": row["content"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def add_list_template(self, name: str, kind: str, content: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = self.conn.execute("""
            INSERT INTO list_templates (name, kind, content, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (name.strip(), kind.strip() or "mixed", content or "", now, now))
        self.conn.commit()
        return int(cur.lastrowid)

    def update_list_template(self, template_id: int, name: str, kind: str, content: str):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE list_templates
            SET name = ?, kind = ?, content = ?, updated_at = ?
            WHERE id = ?
        """, (name.strip(), kind.strip() or "mixed", content or "", now, template_id))
        self.conn.commit()

    def delete_list_template(self, template_id: int):
        self.conn.execute("DELETE FROM list_templates WHERE id = ?", (template_id,))
        self.conn.commit()

    def get_distinct_broadcast_task_targets(self) -> List[str]:
        rows = self.conn.execute("""
            SELECT target_group, MIN(id) AS min_id
            FROM tasks
            WHERE task_type = 'broadcast'
            GROUP BY target_group
            ORDER BY min_id ASC
        """).fetchall()
        return [r["target_group"] for r in rows if r["target_group"]]

    # --- CycleBroadcast ---

    def get_or_create_cycle_campaign(self, name: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        row = self.conn.execute(
            "SELECT id FROM cycle_campaigns WHERE name = ?",
            (name.strip(),),
        ).fetchone()
        if row:
            return int(row["id"])
        cur = self.conn.execute("""
            INSERT INTO cycle_campaigns (name, created_at, updated_at)
            VALUES (?, ?, ?)
        """, (name.strip(), now, now))
        self.conn.commit()
        return int(cur.lastrowid)

    def load_cycle_campaign(self, campaign_id: int) -> dict | None:
        row = self.conn.execute("""
            SELECT * FROM cycle_campaigns WHERE id = ?
        """, (campaign_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def set_cycle_campaign_enabled(self, campaign_id: int, enabled: bool):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE cycle_campaigns
            SET enabled = ?, updated_at = ?
            WHERE id = ?
        """, (int(bool(enabled)), now, campaign_id))
        self.conn.commit()

    def update_cycle_campaign(
        self,
        campaign_id: int,
        targets_source: str,
        template_id: int | None,
        message_source: str,
        message_text: str,
        unique_mode: str,
        enabled: bool,
        account_filter: str = "",
        rotate_after_n_sends: int | None = None,
        send_delay_min_seconds: int | None = None,
        send_delay_max_seconds: int | None = None,
        round_pause_seconds: int | None = None,
        message_template_id: int | None = None,
        default_hours_start: int | None = None,
        default_hours_end: int | None = None,
        default_interval_min_seconds: int | None = None,
        default_interval_max_seconds: int | None = None,
        default_min_new_messages: int | None = None,
        default_fallback_hours: int | None = None,
    ):
        now = datetime.now().isoformat(timespec="seconds")
        if send_delay_min_seconds is None:
            send_delay_min_seconds = 30
        if send_delay_max_seconds is None:
            send_delay_max_seconds = int(send_delay_min_seconds or 0)
        if round_pause_seconds is None:
            round_pause_seconds = 0
        send_delay_min_seconds = max(1, int(send_delay_min_seconds or 1))
        send_delay_max_seconds = max(send_delay_min_seconds, int(send_delay_max_seconds or send_delay_min_seconds))
        round_pause_seconds = max(0, int(round_pause_seconds or 0))
        account_filter = (account_filter or "").strip()
        rotate_after_n_sends = max(0, int(rotate_after_n_sends or 0))
        message_template_id = int(message_template_id) if message_template_id else None
        default_hours_start = max(0, min(23, int(default_hours_start if default_hours_start is not None else 0)))
        default_hours_end = max(0, min(23, int(default_hours_end if default_hours_end is not None else 23)))
        default_interval_min_seconds = max(0, int(default_interval_min_seconds or 0))
        default_interval_max_seconds = max(default_interval_min_seconds, int(default_interval_max_seconds or default_interval_min_seconds))
        default_min_new_messages = max(0, int(default_min_new_messages or 0))
        default_fallback_hours = max(0, int(default_fallback_hours or 0))

        self.conn.execute("""
            UPDATE cycle_campaigns
            SET targets_source = ?, template_id = ?,
                message_source = ?, message_text = ?, unique_mode = ?,
                enabled = ?, updated_at = ?
                , account_filter = ?, rotate_after_n_sends = ?
                , send_delay_min_seconds = ?, send_delay_max_seconds = ?, round_pause_seconds = ?
                , message_template_id = ?
                , default_hours_start = ?, default_hours_end = ?
                , default_interval_min_seconds = ?, default_interval_max_seconds = ?
                , default_min_new_messages = ?, default_fallback_hours = ?
            WHERE id = ?
        """, (
            targets_source, template_id,
            message_source, message_text or "",
            unique_mode, int(enabled), now,
            account_filter, rotate_after_n_sends,
            send_delay_min_seconds, send_delay_max_seconds, round_pause_seconds,
            message_template_id,
            default_hours_start, default_hours_end,
            default_interval_min_seconds, default_interval_max_seconds,
            default_min_new_messages, default_fallback_hours,
            campaign_id,
        ))
        self.conn.commit()

    def update_cycle_campaign_targets_source(
        self,
        campaign_id: int,
        targets_source: str,
        template_id: int | None,
    ):
        """Сохранить источник целей кампании без изменения остальных настроек."""
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE cycle_campaigns
            SET targets_source = ?, template_id = ?, updated_at = ?
            WHERE id = ?
        """, (
            (targets_source or "template").strip(),
            template_id,
            now,
            campaign_id,
        ))
        self.conn.commit()

    def list_cycle_campaigns(self) -> List[dict]:
        rows = self.conn.execute("""
            SELECT id, name, enabled, updated_at
            FROM cycle_campaigns
            ORDER BY updated_at DESC, name ASC
        """).fetchall()
        return [dict(r) for r in rows]

    def rename_cycle_campaign(self, campaign_id: int, new_name: str):
        new_name = (new_name or "").strip()
        if not new_name:
            raise ValueError("empty campaign name")
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE cycle_campaigns
            SET name = ?, updated_at = ?
            WHERE id = ?
        """, (new_name, now, campaign_id))
        self.conn.commit()

    def delete_cycle_campaign(self, campaign_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM cycle_campaign_accounts WHERE campaign_id = ?", (campaign_id,))
        cur.execute("DELETE FROM cycle_targets WHERE campaign_id = ?", (campaign_id,))
        cur.execute("DELETE FROM cycle_state WHERE campaign_id = ?", (campaign_id,))
        cur.execute("DELETE FROM cycle_campaigns WHERE id = ?", (campaign_id,))
        self.conn.commit()
        return (cur.rowcount or 0) > 0

    def get_cycle_campaign_account_phones(self, campaign_id: int) -> List[str]:
        rows = self.conn.execute("""
            SELECT account_phone
            FROM cycle_campaign_accounts
            WHERE campaign_id = ?
            ORDER BY pos ASC
        """, (campaign_id,)).fetchall()
        return [str(r["account_phone"]) for r in rows if r["account_phone"]]

    def set_cycle_campaign_accounts(self, campaign_id: int, account_phones: List[str]):
        now = datetime.now().isoformat(timespec="seconds")
        clean = []
        seen = set()
        for p in (account_phones or []):
            p = (p or "").strip()
            if not p or p in seen:
                continue
            seen.add(p)
            clean.append(p)

        cur = self.conn.cursor()
        cur.execute("DELETE FROM cycle_campaign_accounts WHERE campaign_id = ?", (campaign_id,))
        for pos, phone in enumerate(clean):
            cur.execute("""
                INSERT INTO cycle_campaign_accounts (campaign_id, account_phone, pos, updated_at)
                VALUES (?, ?, ?, ?)
            """, (campaign_id, phone, int(pos), now))
        self.conn.commit()

    def add_cycle_state_stats(self, campaign_id: int, sent_inc: int = 0, error_inc: int = 0, last_error: str = ""):
        now = datetime.now().isoformat(timespec="seconds")
        self.load_cycle_state(campaign_id)
        self.conn.execute("""
            UPDATE cycle_state
            SET sent_total = sent_total + ?,
                error_total = error_total + ?,
                last_error = ?,
                updated_at = ?
            WHERE campaign_id = ?
        """, (
            int(sent_inc or 0),
            int(error_inc or 0),
            (last_error or "")[:500],
            now,
            campaign_id,
        ))
        self.conn.commit()

    def set_cycle_state_account_send_count(self, campaign_id: int, send_count: int):
        now = datetime.now().isoformat(timespec="seconds")
        self.load_cycle_state(campaign_id)
        self.conn.execute("""
            UPDATE cycle_state
            SET last_account_send_count = ?, updated_at = ?
            WHERE campaign_id = ?
        """, (max(0, int(send_count or 0)), now, campaign_id))
        self.conn.commit()

    def replace_cycle_targets(
        self,
        campaign_id: int,
        links: List[str],
        defaults: dict,
    ) -> tuple[int, int]:
        now = datetime.now().isoformat(timespec="seconds")
        clean_links = []
        seen_links = set()
        for raw_link in links:
            link = (raw_link or "").strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            clean_links.append(link)
        keep_set = set(clean_links)

        interval_min_seconds = int(defaults.get("interval_min_seconds", 0) or 0)
        interval_max_seconds = int(defaults.get("interval_max_seconds", 0) or 0)
        if interval_min_seconds <= 0 and int(defaults.get("min_interval_minutes", 0) or 0) > 0:
            interval_min_seconds = int(defaults.get("min_interval_minutes", 0) or 0) * 60
            interval_max_seconds = interval_min_seconds
        if interval_min_seconds < 0:
            interval_min_seconds = 0
        if interval_max_seconds < interval_min_seconds:
            interval_max_seconds = interval_min_seconds

        rows = self.conn.execute("""
            SELECT id, link FROM cycle_targets WHERE campaign_id = ?
        """, (campaign_id,)).fetchall()
        existing_by_link = {r["link"]: int(r["id"]) for r in rows}

        added = 0
        updated = 0
        for pos, link in enumerate(clean_links):
            if link in existing_by_link:
                self.conn.execute("""
                    UPDATE cycle_targets
                    SET pos = ?, updated_at = ?
                    WHERE id = ?
                """, (pos, now, existing_by_link[link]))
                updated += 1
            else:
                self.conn.execute("""
                    INSERT INTO cycle_targets (
                        campaign_id, pos, link,
                        hours_start, hours_end,
                        min_interval_minutes,
                        interval_min_seconds, interval_max_seconds,
                        min_new_messages, fallback_hours,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    campaign_id, pos, link,
                    int(defaults.get("hours_start", 0)),
                    int(defaults.get("hours_end", 23)),
                    int(defaults.get("min_interval_minutes", 0)),
                    interval_min_seconds,
                    interval_max_seconds,
                    int(defaults.get("min_new_messages", 0)),
                    int(defaults.get("fallback_hours", 0)),
                    now,
                ))
                existing_by_link[link] = int(self.conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                added += 1

        for link, row_id in existing_by_link.items():
            if link not in keep_set:
                self.conn.execute("DELETE FROM cycle_targets WHERE id = ?", (row_id,))

        self.conn.commit()
        return added, updated

    def get_cycle_targets(self, campaign_id: int) -> List[dict]:
        rows = self.conn.execute("""
            SELECT *
            FROM cycle_targets
            WHERE campaign_id = ?
            ORDER BY pos ASC
        """, (campaign_id,)).fetchall()
        return [dict(r) for r in rows]

    def update_cycle_target_rules(
        self,
        target_id: int,
        hours_start: int,
        hours_end: int,
        interval_min_seconds: int,
        interval_max_seconds: int,
        min_new_messages: int,
        fallback_hours: int,
    ):
        now = datetime.now().isoformat(timespec="seconds")
        interval_min_seconds = int(interval_min_seconds or 0)
        interval_max_seconds = int(interval_max_seconds or 0)
        if interval_min_seconds < 0:
            interval_min_seconds = 0
        if interval_max_seconds < interval_min_seconds:
            interval_max_seconds = interval_min_seconds

        self.conn.execute("""
            UPDATE cycle_targets
            SET hours_start = ?, hours_end = ?,
                interval_min_seconds = ?, interval_max_seconds = ?,
                min_new_messages = ?, fallback_hours = ?,
                updated_at = ?
            WHERE id = ?
        """, (
            int(hours_start), int(hours_end),
            interval_min_seconds, interval_max_seconds,
            int(min_new_messages),
            int(fallback_hours),
            now, target_id,
        ))
        self.conn.commit()

    def set_cycle_target_status(
        self,
        target_id: int,
        status: str,
        retry_after: str = "",
        last_error: str = "",
    ):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE cycle_targets
            SET status = ?, retry_after = ?, last_error = ?, updated_at = ?
            WHERE id = ?
        """, (status, retry_after or "", last_error[:500], now, target_id))
        self.conn.commit()

    def update_cycle_target_after_send(
        self,
        target_id: int,
        last_sent_at: str,
        last_seen_msg_id: int,
        account_phone: str,
        text_preview: str,
        retry_after: str = "",
    ):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE cycle_targets
            SET last_sent_at = ?, last_seen_msg_id = ?,
                last_account_phone = ?, last_text_preview = ?,
                status = 'active', retry_after = ?, last_error = '',
                updated_at = ?
            WHERE id = ?
        """, (
            last_sent_at, int(last_seen_msg_id),
            account_phone or "", (text_preview or "")[:200],
            retry_after or "",
            now, target_id,
        ))
        self.conn.commit()

    def set_cycle_target_last_seen(self, target_id: int, last_seen_msg_id: int):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            UPDATE cycle_targets
            SET last_seen_msg_id = ?, updated_at = ?
            WHERE id = ?
        """, (int(last_seen_msg_id), now, target_id))
        self.conn.commit()

    def load_cycle_state(self, campaign_id: int) -> dict:
        row = self.conn.execute("""
            SELECT * FROM cycle_state WHERE campaign_id = ?
        """, (campaign_id,)).fetchone()
        if row:
            return dict(row)
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO cycle_state (campaign_id, updated_at)
            VALUES (?, ?)
        """, (campaign_id, now))
        self.conn.commit()
        return {
            "campaign_id": campaign_id,
            "current_pos": 0,
            "last_target_link": "",
            "last_run_at": "",
            "last_account_phone": "",
            "last_text_preview": "",
            "updated_at": now,
        }

    def update_cycle_state(
        self,
        campaign_id: int,
        current_pos: int,
        last_target_link: str = "",
        last_run_at: str = "",
        last_account_phone: str = "",
        last_text_preview: str = "",
    ):
        now = datetime.now().isoformat(timespec="seconds")
        self.conn.execute("""
            INSERT INTO cycle_state (
                campaign_id, current_pos, last_target_link, last_run_at,
                last_account_phone, last_text_preview, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id) DO UPDATE SET
                current_pos = excluded.current_pos,
                last_target_link = excluded.last_target_link,
                last_run_at = excluded.last_run_at,
                last_account_phone = excluded.last_account_phone,
                last_text_preview = excluded.last_text_preview,
                updated_at = excluded.updated_at
        """, (
            campaign_id,
            int(current_pos),
            last_target_link or "",
            last_run_at or "",
            last_account_phone or "",
            (last_text_preview or "")[:200],
            now,
        ))
        self.conn.commit()

    # --- SendLog ---

    def log_send(self, log: SendLog):
        """Записать лог отправки."""
        self.conn.execute("""
            INSERT INTO send_log (account_phone, target_group, message_text, status, error_detail, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            log.account_phone, log.target_group, log.message_text,
            log.status, log.error_detail,
            log.timestamp or datetime.now().isoformat(),
        ))
        self.conn.commit()

    def log_mention(self, account_phone: str, target_group: str, user_ids: List[int], status: str):
        """Записать лог упоминания с user_ids для трекинга."""
        user_ids_str = ",".join(str(uid) for uid in user_ids)
        self.conn.execute("""
            INSERT INTO send_log (account_phone, target_group, message_text, status, error_detail, timestamp)
            VALUES (?, ?, ?, ?, '', ?)
        """, (
            account_phone, target_group, f"mention:{user_ids_str}",
            status, datetime.now().isoformat(),
        ))
        self.conn.commit()

    def get_already_mentioned_user_ids_from_log(self, target_group: str) -> Set[int]:
        """Получить user_id уже упомянутых в целевой группе."""
        rows = self.conn.execute("""
            SELECT message_text FROM send_log
            WHERE target_group = ? AND status = 'sent' AND message_text LIKE 'mention:%'
        """, (target_group,)).fetchall()

        user_ids = set()
        for row in rows:
            text = row["message_text"]
            if text.startswith("mention:"):
                ids_str = text[len("mention:"):]
                for uid in ids_str.split(","):
                    uid = uid.strip()
                    if uid.isdigit():
                        user_ids.add(int(uid))
        return user_ids

    def get_stats(self, days: int = 7) -> dict:
        """Статистика отправок за последние N дней."""
        rows = self.conn.execute("""
            SELECT status, COUNT(*) as cnt FROM send_log
            WHERE timestamp >= date('now', ?)
            GROUP BY status
        """, (f"-{days} days",)).fetchall()

        stats = {"total": 0}
        for row in rows:
            stats[row["status"]] = row["cnt"]
            stats["total"] += row["cnt"]

        if self._table_exists("publications_log"):
            ads_rows = self.conn.execute("""
                SELECT
                    CASE
                        WHEN status = 'ok' THEN 'sent'
                        WHEN status = 'slow_mode' THEN 'flood_wait'
                        WHEN status IN ('forbidden', 'not_member') THEN 'no_permission'
                        ELSE status
                    END AS status,
                    COUNT(*) as cnt
                FROM publications_log
                WHERE time >= date('now', ?)
                GROUP BY
                    CASE
                        WHEN status = 'ok' THEN 'sent'
                        WHEN status = 'slow_mode' THEN 'flood_wait'
                        WHEN status IN ('forbidden', 'not_member') THEN 'no_permission'
                        ELSE status
                    END
            """, (f"-{days} days",)).fetchall()
            for row in ads_rows:
                stats[row["status"]] = stats.get(row["status"], 0) + row["cnt"]
                stats["total"] += row["cnt"]
        return stats

    def get_per_account_stats(self, days: int = 7) -> List[dict]:
        """Per-account топ ошибок за N дней (для dashboard)."""
        rows = [dict(r) for r in self.conn.execute("""
            SELECT account_phone, status, COUNT(*) as cnt FROM send_log
            WHERE timestamp >= date('now', ?)
            GROUP BY account_phone, status
            ORDER BY account_phone, cnt DESC
        """, (f"-{days} days",)).fetchall()]

        if self._table_exists("publications_log"):
            ads_rows = self.conn.execute("""
                SELECT
                    account_phone,
                    CASE
                        WHEN status = 'ok' THEN 'sent'
                        WHEN status = 'slow_mode' THEN 'flood_wait'
                        WHEN status IN ('forbidden', 'not_member') THEN 'no_permission'
                        ELSE status
                    END AS status,
                    COUNT(*) as cnt
                FROM publications_log
                WHERE time >= date('now', ?)
                GROUP BY account_phone,
                    CASE
                        WHEN status = 'ok' THEN 'sent'
                        WHEN status = 'slow_mode' THEN 'flood_wait'
                        WHEN status IN ('forbidden', 'not_member') THEN 'no_permission'
                        ELSE status
                    END
            """, (f"-{days} days",)).fetchall()
            rows.extend(dict(r) for r in ads_rows)

        combined: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (row["account_phone"], row["status"])
            combined[key] = combined.get(key, 0) + int(row["cnt"])

        return [
            {"phone": phone, "status": status, "count": count}
            for (phone, status), count in sorted(combined.items())
        ]

    # --- Proxy pool ---

    def add_proxies_to_pool(self, proxies: List[str]) -> int:
        """Добавить прокси в пул (идемпотентно). Возвращает количество добавленных/обновлённых."""
        cleaned = []
        for p in proxies or []:
            s = (p or "").strip()
            if not s:
                continue
            cleaned.append(s)
        if not cleaned:
            return 0

        now = datetime.now().isoformat(timespec="seconds")
        changed = 0
        for p in cleaned:
            cur = self.conn.execute("""
                INSERT INTO proxy_pool(proxy, created_at, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(proxy) DO UPDATE SET updated_at=excluded.updated_at
            """, (p, now, now))
            changed += cur.rowcount or 0
        self.conn.commit()
        return changed

    def get_proxy_pool(self) -> List[str]:
        rows = self.conn.execute("""
            SELECT proxy FROM proxy_pool
            ORDER BY updated_at DESC
        """).fetchall()
        return [r["proxy"] for r in rows]

    def delete_proxy_from_pool(self, proxy: str) -> bool:
        proxy = (proxy or "").strip()
        if not proxy:
            return False
        cur = self.conn.execute("DELETE FROM proxy_pool WHERE proxy = ?", (proxy,))
        self.conn.commit()
        return (cur.rowcount or 0) > 0

    def clear_proxy_pool(self) -> int:
        cur = self.conn.execute("DELETE FROM proxy_pool")
        self.conn.commit()
        return cur.rowcount or 0

    def close(self):
        self.conn.close()
