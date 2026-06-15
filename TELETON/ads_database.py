"""
ads_database.py — работа с БД для планировщика объявлений.

Отдельный класс AdsDB, подключается к тому же SQLite-файлу что и
существующий Database, но работает только с новыми таблицами:
  ads, groups_targets, ads_adaptations, ads_groups,
  publications_log, required_subs, scheduler_settings

Не трогает существующие таблицы (accounts, parsed_users, tasks,
send_log, matched_posts). Все новые таблицы создаются через
CREATE TABLE IF NOT EXISTS.
"""

import os
import sqlite3
from datetime import datetime
from typing import List, Optional

from ads_models import (
    Ad, GroupTarget, Adaptation, PublicationLog, RequiredSub,
    SchedulerSettings,
    GROUP_STATUS_ACTIVE,
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AdsDB:
    def __init__(self, db_path: str = "data/teleton.db"):
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # FK нужны для ON DELETE CASCADE
        self.conn.execute("PRAGMA foreign_keys = ON")
        # WAL снижает contention между Database и AdsDB (одна база)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def close(self):
        self.conn.close()

    def _create_tables(self):
        cur = self.conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL DEFAULT '',
                text_base TEXT NOT NULL DEFAULT '',
                media_path TEXT DEFAULT '',
                category TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                account_phone TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS groups_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL UNIQUE,
                title TEXT DEFAULT '',
                category TEXT DEFAULT '',
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                interval_minutes_max INTEGER NOT NULL DEFAULT 0,
                hours_start INTEGER NOT NULL DEFAULT 0,
                hours_end INTEGER NOT NULL DEFAULT 23,
                notes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                join_status TEXT NOT NULL DEFAULT 'unknown',
                retry_after TEXT DEFAULT '',
                next_allowed_at TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ads_adaptations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                text TEXT NOT NULL DEFAULT '',
                adaptation_prompt TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (ad_id) REFERENCES ads(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES groups_targets(id) ON DELETE CASCADE,
                UNIQUE (ad_id, group_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ads_groups (
                ad_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (ad_id, group_id),
                FOREIGN KEY (ad_id) REFERENCES ads(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES groups_targets(id) ON DELETE CASCADE
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS publications_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL,
                group_id INTEGER NOT NULL,
                account_phone TEXT NOT NULL DEFAULT '',
                time TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                error_text TEXT DEFAULT '',
                message_id INTEGER
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS required_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                channel_link TEXT NOT NULL,
                is_joined INTEGER NOT NULL DEFAULT 0,
                last_checked TEXT DEFAULT '',
                FOREIGN KEY (group_id) REFERENCES groups_targets(id) ON DELETE CASCADE,
                UNIQUE (group_id, channel_link)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)

        self.conn.commit()

        # Миграция схемы для существующих БД
        self._migrate_schema_v2()
        self._migrate_schema_v3()

    def _migrate_schema_v2(self):
        """Миграция: добавляет next_allowed_at и interval_minutes_max в
        groups_targets, если колонок ещё нет. Идемпотентна — повторный вызов
        не ломается."""
        cur = self.conn.execute("PRAGMA table_info(groups_targets)")
        existing_cols = {row["name"] for row in cur.fetchall()}
        if "next_allowed_at" not in existing_cols:
            self.conn.execute(
                "ALTER TABLE groups_targets "
                "ADD COLUMN next_allowed_at TEXT DEFAULT ''"
            )
            self.conn.commit()
        if "interval_minutes_max" not in existing_cols:
            self.conn.execute(
                "ALTER TABLE groups_targets "
                "ADD COLUMN interval_minutes_max INTEGER NOT NULL DEFAULT 0"
            )
            self.conn.commit()

    def _migrate_schema_v3(self):
        """Миграция: создаёт таблицу pending_device_terminations.
        Хранит запланированные удаления чужих сессий аккаунтов.
        Записи остаются после выполнения (status='done'/'failed')
        для истории; чистятся старее 30 дней при старте GUI."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_device_terminations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_phone TEXT NOT NULL,
                auth_hashes TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT DEFAULT ''
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_pending_device_status_scheduled
            ON pending_device_terminations(status, scheduled_at)
        """)
        self.conn.commit()

    # =========================================================
    # Ads — объявления
    # =========================================================

    def add_ad(self, ad: Ad) -> int:
        now = _now()
        cur = self.conn.execute("""
            INSERT INTO ads (title, text_base, media_path, category, active,
                             account_phone, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (ad.title, ad.text_base, ad.media_path, ad.category,
              int(ad.active), ad.account_phone, now, now))
        self.conn.commit()
        return cur.lastrowid

    def get_ad(self, ad_id: int) -> Optional[Ad]:
        row = self.conn.execute(
            "SELECT * FROM ads WHERE id = ?", (ad_id,)).fetchone()
        return self._row_to_ad(row) if row else None

    def get_all_ads(self) -> List[Ad]:
        rows = self.conn.execute(
            "SELECT * FROM ads ORDER BY id DESC").fetchall()
        return [self._row_to_ad(r) for r in rows]

    def get_active_ads(self) -> List[Ad]:
        rows = self.conn.execute(
            "SELECT * FROM ads WHERE active = 1 ORDER BY id").fetchall()
        return [self._row_to_ad(r) for r in rows]

    def update_ad(self, ad: Ad):
        if ad.id is None:
            raise ValueError("Ad.id is required for update")
        self.conn.execute("""
            UPDATE ads SET title=?, text_base=?, media_path=?, category=?,
                           active=?, account_phone=?, updated_at=?
            WHERE id=?
        """, (ad.title, ad.text_base, ad.media_path, ad.category,
              int(ad.active), ad.account_phone, _now(), ad.id))
        self.conn.commit()

    def delete_ad(self, ad_id: int):
        self.conn.execute("DELETE FROM ads WHERE id = ?", (ad_id,))
        self.conn.commit()

    @staticmethod
    def _row_to_ad(row) -> Ad:
        return Ad(
            id=row["id"],
            title=row["title"],
            text_base=row["text_base"],
            media_path=row["media_path"],
            category=row["category"],
            active=bool(row["active"]),
            account_phone=row["account_phone"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # =========================================================
    # Groups — группы-назначения
    # =========================================================

    def add_group(self, group: GroupTarget) -> int:
        cur = self.conn.execute("""
            INSERT INTO groups_targets (link, title, category, interval_minutes,
                interval_minutes_max, hours_start, hours_end, notes, status,
                join_status, retry_after, next_allowed_at, last_error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (group.link, group.title, group.category, group.interval_minutes,
              group.interval_minutes_max, group.hours_start, group.hours_end,
              group.notes, group.status, group.join_status, group.retry_after,
              group.next_allowed_at, group.last_error, _now()))
        self.conn.commit()
        return cur.lastrowid

    def get_group(self, group_id: int) -> Optional[GroupTarget]:
        row = self.conn.execute(
            "SELECT * FROM groups_targets WHERE id = ?", (group_id,)).fetchone()
        return self._row_to_group(row) if row else None

    def get_group_by_link(self, link: str) -> Optional[GroupTarget]:
        row = self.conn.execute(
            "SELECT * FROM groups_targets WHERE link = ?", (link,)).fetchone()
        return self._row_to_group(row) if row else None

    def get_all_groups(self) -> List[GroupTarget]:
        rows = self.conn.execute(
            "SELECT * FROM groups_targets ORDER BY id").fetchall()
        return [self._row_to_group(r) for r in rows]

    def get_active_groups(self) -> List[GroupTarget]:
        rows = self.conn.execute(
            "SELECT * FROM groups_targets WHERE status = ? ORDER BY id",
            (GROUP_STATUS_ACTIVE,)).fetchall()
        return [self._row_to_group(r) for r in rows]

    def update_group(self, group: GroupTarget):
        """Обновить группу. Если изменился interval_minutes или interval_minutes_max —
        сбрасываем next_allowed_at, чтобы новые настройки применились немедленно,
        а не после старого тайм-аута."""
        if group.id is None:
            raise ValueError("GroupTarget.id is required for update")

        # Читаем старые значения интервала
        old_row = self.conn.execute(
            "SELECT interval_minutes, interval_minutes_max "
            "FROM groups_targets WHERE id=?",
            (group.id,)
        ).fetchone()

        interval_changed = (
            old_row is not None and (
                old_row["interval_minutes"] != group.interval_minutes
                or old_row["interval_minutes_max"] != group.interval_minutes_max
            )
        )

        # Если интервал изменился — обнуляем next_allowed_at
        new_next_allowed = "" if interval_changed else group.next_allowed_at

        self.conn.execute("""
            UPDATE groups_targets SET link=?, title=?, category=?,
                interval_minutes=?, interval_minutes_max=?,
                hours_start=?, hours_end=?, notes=?,
                status=?, join_status=?, retry_after=?, next_allowed_at=?,
                last_error=?
            WHERE id=?
        """, (group.link, group.title, group.category, group.interval_minutes,
              group.interval_minutes_max, group.hours_start, group.hours_end,
              group.notes, group.status, group.join_status, group.retry_after,
              new_next_allowed, group.last_error, group.id))
        self.conn.commit()

    def delete_group(self, group_id: int):
        self.conn.execute("DELETE FROM groups_targets WHERE id = ?", (group_id,))
        self.conn.commit()

    def set_group_retry_after(self, group_id: int, retry_after_iso: str,
                              last_error: str = ""):
        """Обновляет только retry_after и last_error — вызывается из publisher."""
        self.conn.execute("""
            UPDATE groups_targets SET retry_after=?, last_error=? WHERE id=?
        """, (retry_after_iso, last_error, group_id))
        self.conn.commit()

    def set_group_next_allowed_at(self, group_id: int, next_allowed_iso: str):
        """Обновляет только next_allowed_at — вызывается планировщиком
        после успешной публикации для установки следующего разрешённого времени
        (рандомный интервал внутри настроенного диапазона).

        Симметричен set_group_retry_after, но назначение другое:
          - retry_after       — запрет от Telegram (flood, forbidden, slow_mode)
          - next_allowed_at   — наш собственный рандомный интервал между публикациями
        """
        self.conn.execute(
            "UPDATE groups_targets SET next_allowed_at=? WHERE id=?",
            (next_allowed_iso, group_id))
        self.conn.commit()

    def set_group_status(self, group_id: int, status: str, last_error: str = ""):
        self.conn.execute("""
            UPDATE groups_targets SET status=?, last_error=? WHERE id=?
        """, (status, last_error, group_id))
        self.conn.commit()

    def set_group_join_status(self, group_id: int, join_status: str):
        self.conn.execute(
            "UPDATE groups_targets SET join_status=? WHERE id=?",
            (join_status, group_id))
        self.conn.commit()

    @staticmethod
    def _row_to_group(row) -> GroupTarget:
        # next_allowed_at и interval_minutes_max могут отсутствовать в старых БД
        # до миграции — берём через try/except с fallback
        try:
            next_allowed = row["next_allowed_at"] or ""
        except (IndexError, KeyError):
            next_allowed = ""
        try:
            interval_max = row["interval_minutes_max"] or 0
        except (IndexError, KeyError):
            interval_max = 0
        return GroupTarget(
            id=row["id"],
            link=row["link"],
            title=row["title"],
            category=row["category"],
            interval_minutes=row["interval_minutes"],
            interval_minutes_max=interval_max,
            hours_start=row["hours_start"],
            hours_end=row["hours_end"],
            notes=row["notes"],
            status=row["status"],
            join_status=row["join_status"],
            retry_after=row["retry_after"],
            next_allowed_at=next_allowed,
            last_error=row["last_error"],
            created_at=row["created_at"],
        )

    # =========================================================
    # ads_groups — связи объявлений с группами
    # =========================================================

    def link_ad_to_group(self, ad_id: int, group_id: int):
        self.conn.execute("""
            INSERT OR IGNORE INTO ads_groups (ad_id, group_id) VALUES (?, ?)
        """, (ad_id, group_id))
        self.conn.commit()

    def unlink_ad_from_group(self, ad_id: int, group_id: int):
        self.conn.execute(
            "DELETE FROM ads_groups WHERE ad_id=? AND group_id=?",
            (ad_id, group_id))
        self.conn.commit()

    def get_groups_for_ad(self, ad_id: int) -> List[GroupTarget]:
        rows = self.conn.execute("""
            SELECT g.* FROM groups_targets g
            JOIN ads_groups ag ON ag.group_id = g.id
            WHERE ag.ad_id = ?
            ORDER BY g.id
        """, (ad_id,)).fetchall()
        return [self._row_to_group(r) for r in rows]

    def get_ads_for_group(self, group_id: int) -> List[Ad]:
        rows = self.conn.execute("""
            SELECT a.* FROM ads a
            JOIN ads_groups ag ON ag.ad_id = a.id
            WHERE ag.group_id = ?
            ORDER BY a.id
        """, (group_id,)).fetchall()
        return [self._row_to_ad(r) for r in rows]

    # =========================================================
    # Adaptations — адаптированные тексты под группу
    # =========================================================

    def set_adaptation(self, ad_id: int, group_id: int,
                       text: str, prompt: str = "") -> int:
        """Создать или заменить адаптацию."""
        self.conn.execute("""
            INSERT INTO ads_adaptations (ad_id, group_id, text,
                                          adaptation_prompt, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ad_id, group_id) DO UPDATE SET
                text = excluded.text,
                adaptation_prompt = excluded.adaptation_prompt,
                created_at = excluded.created_at
        """, (ad_id, group_id, text, prompt, _now()))
        self.conn.commit()
        row = self.conn.execute("""
            SELECT id FROM ads_adaptations WHERE ad_id=? AND group_id=?
        """, (ad_id, group_id)).fetchone()
        return row["id"] if row else 0

    def get_adaptation(self, ad_id: int, group_id: int) -> Optional[Adaptation]:
        row = self.conn.execute("""
            SELECT * FROM ads_adaptations WHERE ad_id=? AND group_id=?
        """, (ad_id, group_id)).fetchone()
        if not row:
            return None
        return Adaptation(
            id=row["id"],
            ad_id=row["ad_id"],
            group_id=row["group_id"],
            text=row["text"],
            adaptation_prompt=row["adaptation_prompt"],
            created_at=row["created_at"],
        )

    def delete_adaptation(self, ad_id: int, group_id: int):
        self.conn.execute("""
            DELETE FROM ads_adaptations WHERE ad_id=? AND group_id=?
        """, (ad_id, group_id))
        self.conn.commit()

    # =========================================================
    # publications_log — журнал публикаций
    # =========================================================

    def add_publication_log(self, log: PublicationLog) -> int:
        cur = self.conn.execute("""
            INSERT INTO publications_log (ad_id, group_id, account_phone,
                                           time, status, error_text, message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (log.ad_id, log.group_id, log.account_phone,
              log.time or _now(), log.status, log.error_text, log.message_id))
        self.conn.commit()
        return cur.lastrowid

    def get_publications_log(self, limit: int = 500,
                             ad_id: Optional[int] = None,
                             group_id: Optional[int] = None,
                             status: Optional[str] = None) -> List[PublicationLog]:
        query = "SELECT * FROM publications_log WHERE 1=1"
        params = []
        if ad_id is not None:
            query += " AND ad_id = ?"; params.append(ad_id)
        if group_id is not None:
            query += " AND group_id = ?"; params.append(group_id)
        if status is not None:
            query += " AND status = ?"; params.append(status)
        query += " ORDER BY id DESC LIMIT ?"; params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [PublicationLog(
            id=r["id"], ad_id=r["ad_id"], group_id=r["group_id"],
            account_phone=r["account_phone"], time=r["time"],
            status=r["status"], error_text=r["error_text"],
            message_id=r["message_id"],
        ) for r in rows]

    def get_last_publication_to_group(self, group_id: int,
                                      only_ok: bool = True) -> Optional[PublicationLog]:
        """Последняя успешная (или любая) публикация в группу."""
        query = "SELECT * FROM publications_log WHERE group_id = ?"
        params = [group_id]
        if only_ok:
            query += " AND status = 'ok'"
        query += " ORDER BY id DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        if not row:
            return None
        return PublicationLog(
            id=row["id"], ad_id=row["ad_id"], group_id=row["group_id"],
            account_phone=row["account_phone"], time=row["time"],
            status=row["status"], error_text=row["error_text"],
            message_id=row["message_id"],
        )

    def count_publications_today(self, account_phone: str,
                                 only_ok: bool = True) -> int:
        """Счётчик публикаций сегодня с конкретного аккаунта (для dailylimit)."""
        today = datetime.now().date().isoformat()
        query = """
            SELECT COUNT(*) as c FROM publications_log
            WHERE account_phone = ? AND time >= ?
        """
        params = [account_phone, today]
        if only_ok:
            query += " AND status = 'ok'"
        row = self.conn.execute(query, params).fetchone()
        return row["c"]

    # =========================================================
    # required_subs — обязательные подписки
    # =========================================================

    def add_required_sub(self, group_id: int, channel_link: str) -> int:
        self.conn.execute("""
            INSERT OR IGNORE INTO required_subs (group_id, channel_link, is_joined)
            VALUES (?, ?, 0)
        """, (group_id, channel_link))
        self.conn.commit()
        row = self.conn.execute("""
            SELECT id FROM required_subs WHERE group_id=? AND channel_link=?
        """, (group_id, channel_link)).fetchone()
        return row["id"] if row else 0

    def delete_required_sub(self, group_id: int, channel_link: str):
        self.conn.execute("""
            DELETE FROM required_subs WHERE group_id=? AND channel_link=?
        """, (group_id, channel_link))
        self.conn.commit()

    def get_required_subs_for_group(self, group_id: int) -> List[RequiredSub]:
        rows = self.conn.execute("""
            SELECT * FROM required_subs WHERE group_id = ? ORDER BY id
        """, (group_id,)).fetchall()
        return [RequiredSub(
            id=r["id"], group_id=r["group_id"],
            channel_link=r["channel_link"],
            is_joined=bool(r["is_joined"]),
            last_checked=r["last_checked"],
        ) for r in rows]

    def set_sub_joined(self, group_id: int, channel_link: str,
                       is_joined: bool):
        self.conn.execute("""
            UPDATE required_subs SET is_joined=?, last_checked=?
            WHERE group_id=? AND channel_link=?
        """, (int(is_joined), _now(), group_id, channel_link))
        self.conn.commit()

    # =========================================================
    # scheduler_settings — настройки планировщика (key-value)
    # =========================================================

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM scheduler_settings WHERE key = ?",
            (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str):
        self.conn.execute("""
            INSERT INTO scheduler_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, str(value)))
        self.conn.commit()

    def load_scheduler_settings(self) -> SchedulerSettings:
        """Загрузить все настройки планировщика, дополнив дефолтами.

        Включает одноразовую миграцию legacy-ключей (publication_interval_seconds,
        join_interval_seconds) в новые min/max: старое значение = min, max = 2 × min.
        Миграция не выполняется если новые ключи уже заданы.
        """
        s = SchedulerSettings()

        # --- Миграция legacy-ключей в новые min/max (одноразовая) ---
        legacy_migrations = (
            ("publication_interval_seconds",
             "publication_interval_min_seconds",
             "publication_interval_max_seconds"),
            ("join_interval_seconds",
             "join_interval_min_seconds",
             "join_interval_max_seconds"),
        )
        for legacy_key, min_key, max_key in legacy_migrations:
            legacy_val = self.get_setting(legacy_key)
            if legacy_val and not self.get_setting(min_key):
                try:
                    v = int(legacy_val)
                    self.set_setting(min_key, str(v))
                    self.set_setting(max_key, str(v * 2))
                except ValueError:
                    pass

        # --- Загрузка всех int-полей (legacy + новые) ---
        int_fields = (
            # Legacy
            "publication_interval_seconds",
            "join_interval_seconds",
            "daily_publication_limit",
            "daily_join_limit",
            # Ads — новые min/max
            "publication_interval_min_seconds",
            "publication_interval_max_seconds",
            "join_interval_min_seconds",
            "join_interval_max_seconds",
            # Broadcast / mention / DM / group_check
            "broadcast_delay_min_seconds",
            "broadcast_delay_max_seconds",
            "mention_delay_min_seconds",
            "mention_delay_max_seconds",
            "dm_delay_min_seconds",
            "dm_delay_max_seconds",
            "group_check_join_delay_min_seconds",
            "group_check_join_delay_max_seconds",
            # Импорт TData
            "tdata_connect_timeout_seconds",
            "tdata_get_me_timeout_seconds",
            "tdata_flood_max_wait_seconds",
            "tdata_flood_jitter_min_seconds",
            "tdata_flood_jitter_max_seconds",
            # Управление устройствами
            "device_terminate_delay_min_seconds",
            "device_terminate_delay_max_seconds",
            "device_terminate_default_schedule_hours",
        )
        for field_name in int_fields:
            val = self.get_setting(field_name)
            if val:
                try:
                    setattr(s, field_name, int(val))
                except ValueError:
                    pass
        for field_name in ("ai_provider", "ai_model_openai", "ai_model_groq"):
            val = self.get_setting(field_name)
            if val:
                setattr(s, field_name, val)
        return s

    def save_scheduler_settings(self, s: SchedulerSettings):
        # Legacy-ключи (для обратной совместимости со старым кодом)
        self.set_setting("publication_interval_seconds",
                         str(s.publication_interval_seconds))
        self.set_setting("daily_publication_limit", str(s.daily_publication_limit))
        self.set_setting("join_interval_seconds", str(s.join_interval_seconds))
        self.set_setting("daily_join_limit", str(s.daily_join_limit))
        # Новые min/max
        self.set_setting("publication_interval_min_seconds",
                         str(s.publication_interval_min_seconds))
        self.set_setting("publication_interval_max_seconds",
                         str(s.publication_interval_max_seconds))
        self.set_setting("join_interval_min_seconds",
                         str(s.join_interval_min_seconds))
        self.set_setting("join_interval_max_seconds",
                         str(s.join_interval_max_seconds))
        self.set_setting("broadcast_delay_min_seconds",
                         str(s.broadcast_delay_min_seconds))
        self.set_setting("broadcast_delay_max_seconds",
                         str(s.broadcast_delay_max_seconds))
        self.set_setting("mention_delay_min_seconds",
                         str(s.mention_delay_min_seconds))
        self.set_setting("mention_delay_max_seconds",
                         str(s.mention_delay_max_seconds))
        self.set_setting("dm_delay_min_seconds", str(s.dm_delay_min_seconds))
        self.set_setting("dm_delay_max_seconds", str(s.dm_delay_max_seconds))
        self.set_setting("group_check_join_delay_min_seconds",
                         str(s.group_check_join_delay_min_seconds))
        self.set_setting("group_check_join_delay_max_seconds",
                         str(s.group_check_join_delay_max_seconds))
        # Импорт TData
        self.set_setting("tdata_connect_timeout_seconds",
                         str(s.tdata_connect_timeout_seconds))
        self.set_setting("tdata_get_me_timeout_seconds",
                         str(s.tdata_get_me_timeout_seconds))
        self.set_setting("tdata_flood_max_wait_seconds",
                         str(s.tdata_flood_max_wait_seconds))
        self.set_setting("tdata_flood_jitter_min_seconds",
                         str(s.tdata_flood_jitter_min_seconds))
        self.set_setting("tdata_flood_jitter_max_seconds",
                         str(s.tdata_flood_jitter_max_seconds))
        # Управление устройствами
        self.set_setting("device_terminate_delay_min_seconds",
                         str(s.device_terminate_delay_min_seconds))
        self.set_setting("device_terminate_delay_max_seconds",
                         str(s.device_terminate_delay_max_seconds))
        self.set_setting("device_terminate_default_schedule_hours",
                         str(s.device_terminate_default_schedule_hours))
        # AI
        self.set_setting("ai_provider", s.ai_provider)
        self.set_setting("ai_model_openai", s.ai_model_openai)
        self.set_setting("ai_model_groq", s.ai_model_groq)

    # =========================================================
    # Pending device terminations (расписание удаления чужих сессий)
    # =========================================================

    def add_pending_device_termination(self, account_phone: str,
                                        auth_hashes: list,
                                        scheduled_at_iso: str) -> int:
        """Запланировать удаление сессий аккаунта на момент scheduled_at_iso.
        auth_hashes — список int hash'ей (Authorization.hash из Telethon).
        Возвращает id новой записи."""
        import json
        from datetime import datetime
        cur = self.conn.execute("""
            INSERT INTO pending_device_terminations
            (account_phone, auth_hashes, scheduled_at, created_at, status)
            VALUES (?, ?, ?, ?, 'pending')
        """, (account_phone, json.dumps(auth_hashes), scheduled_at_iso,
              datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        return cur.lastrowid

    def get_due_device_terminations(self, now_iso: str) -> list:
        """Вернуть pending-задачи у которых scheduled_at <= now_iso.
        Возвращает список dict: {id, account_phone, auth_hashes (list), scheduled_at}."""
        import json
        rows = self.conn.execute("""
            SELECT id, account_phone, auth_hashes, scheduled_at
            FROM pending_device_terminations
            WHERE status = 'pending' AND scheduled_at <= ?
            ORDER BY scheduled_at ASC
        """, (now_iso,)).fetchall()
        result = []
        for r in rows:
            try:
                hashes = json.loads(r["auth_hashes"])
            except (json.JSONDecodeError, TypeError):
                hashes = []
            result.append({
                "id": r["id"],
                "account_phone": r["account_phone"],
                "auth_hashes": hashes,
                "scheduled_at": r["scheduled_at"],
            })
        return result

    def mark_device_termination_done(self, task_id: int):
        self.conn.execute(
            "UPDATE pending_device_terminations SET status='done' WHERE id=?",
            (task_id,))
        self.conn.commit()

    def mark_device_termination_failed(self, task_id: int, error: str):
        self.conn.execute("""
            UPDATE pending_device_terminations
            SET status='failed', last_error=?
            WHERE id=?
        """, (error[:500], task_id))
        self.conn.commit()

    def cleanup_old_device_terminations(self, days_old: int = 30):
        """Чистка истории старее N дней (вызывается при старте GUI)."""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days_old)).isoformat()
        self.conn.execute("""
            DELETE FROM pending_device_terminations
            WHERE status IN ('done', 'failed') AND created_at < ?
        """, (cutoff,))
        self.conn.commit()
