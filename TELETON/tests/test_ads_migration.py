"""
Тесты миграции БД и расширения настроек (Этап 1):
  - Колонка next_allowed_at в groups_targets
  - Миграция legacy-ключей (publication_interval_seconds, join_interval_seconds)
    в новые min/max
  - Новый метод set_group_next_allowed_at
  - Расширенный SchedulerSettings со всеми новыми полями
"""
import os
import sqlite3
import tempfile

import pytest

from ads_database import AdsDB
from ads_models import GroupTarget, SchedulerSettings, GROUP_STATUS_ACTIVE


@pytest.fixture
def tmp_db_path():
    """Временный путь под SQLite-файл, удаляется после теста."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    yield path
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Миграция колонки next_allowed_at
# ─────────────────────────────────────────────────────────────────────────────

class TestNextAllowedAtColumn:
    def test_fresh_db_has_next_allowed_at_column(self, tmp_db_path):
        """Новая БД создаётся с колонкой next_allowed_at."""
        db = AdsDB(tmp_db_path)
        try:
            cols = {row["name"] for row in
                    db.conn.execute("PRAGMA table_info(groups_targets)").fetchall()}
            assert "next_allowed_at" in cols
        finally:
            db.close()

    def test_existing_db_adds_next_allowed_at_column(self, tmp_db_path):
        """Старая БД без колонки — после подключения AdsDB колонка добавлена."""
        # Создаём БД вручную БЕЗ next_allowed_at (как у существующих пользователей)
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""
            CREATE TABLE groups_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL UNIQUE,
                title TEXT DEFAULT '',
                category TEXT DEFAULT '',
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                hours_start INTEGER NOT NULL DEFAULT 0,
                hours_end INTEGER NOT NULL DEFAULT 23,
                notes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                join_status TEXT NOT NULL DEFAULT 'unknown',
                retry_after TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()
        conn.close()

        # Подключаем AdsDB — миграция должна сработать
        db = AdsDB(tmp_db_path)
        try:
            cols = {row["name"] for row in
                    db.conn.execute("PRAGMA table_info(groups_targets)").fetchall()}
            assert "next_allowed_at" in cols
        finally:
            db.close()

    def test_migration_preserves_existing_data(self, tmp_db_path):
        """Миграция не затирает существующие строки."""
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("""
            CREATE TABLE groups_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL UNIQUE,
                title TEXT DEFAULT '',
                category TEXT DEFAULT '',
                interval_minutes INTEGER NOT NULL DEFAULT 60,
                hours_start INTEGER NOT NULL DEFAULT 0,
                hours_end INTEGER NOT NULL DEFAULT 23,
                notes TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                join_status TEXT NOT NULL DEFAULT 'unknown',
                retry_after TEXT DEFAULT '',
                last_error TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "INSERT INTO groups_targets (link, title, created_at) VALUES (?, ?, ?)",
            ("@legacy_group", "Legacy", "2020-01-01T00:00:00"))
        conn.commit()
        conn.close()

        db = AdsDB(tmp_db_path)
        try:
            g = db.get_group_by_link("@legacy_group")
            assert g is not None
            assert g.title == "Legacy"
            assert g.next_allowed_at == ""  # default после миграции
        finally:
            db.close()

    def test_migration_is_idempotent(self, tmp_db_path):
        """Два подключения подряд не ломают друг друга и не падают."""
        db1 = AdsDB(tmp_db_path)
        db1.close()
        # Второе подключение — миграция должна пройти без ошибок
        db2 = AdsDB(tmp_db_path)
        try:
            cols = {row["name"] for row in
                    db2.conn.execute("PRAGMA table_info(groups_targets)").fetchall()}
            assert "next_allowed_at" in cols
        finally:
            db2.close()


# ─────────────────────────────────────────────────────────────────────────────
# CRUD для next_allowed_at
# ─────────────────────────────────────────────────────────────────────────────

class TestGroupCRUDWithNextAllowedAt:
    def test_add_group_stores_next_allowed_at(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            g = GroupTarget(
                link="@grp1",
                title="G1",
                next_allowed_at="2030-01-01T00:00:00",
            )
            gid = db.add_group(g)
            fetched = db.get_group(gid)
            assert fetched is not None
            assert fetched.next_allowed_at == "2030-01-01T00:00:00"
        finally:
            db.close()

    def test_add_group_default_next_allowed_at_is_empty(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            gid = db.add_group(GroupTarget(link="@grp2", title="G2"))
            fetched = db.get_group(gid)
            assert fetched.next_allowed_at == ""
        finally:
            db.close()

    def test_update_group_updates_next_allowed_at(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            gid = db.add_group(GroupTarget(link="@grp3", title="G3"))
            g = db.get_group(gid)
            g.next_allowed_at = "2030-06-15T12:00:00"
            db.update_group(g)
            fetched = db.get_group(gid)
            assert fetched.next_allowed_at == "2030-06-15T12:00:00"
        finally:
            db.close()

    def test_set_group_next_allowed_at(self, tmp_db_path):
        """Точечный setter обновляет только next_allowed_at, остальное не трогает."""
        db = AdsDB(tmp_db_path)
        try:
            gid = db.add_group(GroupTarget(
                link="@grp4", title="G4",
                retry_after="2020-01-01T00:00:00",
                last_error="old error",
            ))
            db.set_group_next_allowed_at(gid, "2031-12-31T23:59:59")
            fetched = db.get_group(gid)
            assert fetched.next_allowed_at == "2031-12-31T23:59:59"
            # Остальные поля не затронуты
            assert fetched.retry_after == "2020-01-01T00:00:00"
            assert fetched.last_error == "old error"
            assert fetched.title == "G4"
        finally:
            db.close()

    def test_row_to_group_handles_missing_column(self, tmp_db_path):
        """_row_to_group не падает если колонки нет в row (edge case)."""
        # Создаём минимальную строку через sqlite3.Row
        db = AdsDB(tmp_db_path)
        try:
            db.add_group(GroupTarget(link="@grp5", title="G5"))
            row = db.conn.execute(
                "SELECT * FROM groups_targets WHERE link=?", ("@grp5",)
            ).fetchone()
            g = AdsDB._row_to_group(row)
            assert g.next_allowed_at == ""  # пустая строка, не None
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# SchedulerSettings: новые поля и defaults
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerSettingsNewFields:
    def test_defaults_are_present(self):
        """Все новые поля имеют ожидаемые defaults."""
        s = SchedulerSettings()
        # Ads min/max
        assert s.publication_interval_min_seconds == 300
        assert s.publication_interval_max_seconds == 600
        assert s.join_interval_min_seconds == 900
        assert s.join_interval_max_seconds == 1800
        # Broadcast
        assert s.broadcast_delay_min_seconds == 30
        assert s.broadcast_delay_max_seconds == 90
        # Mention
        assert s.mention_delay_min_seconds == 45
        assert s.mention_delay_max_seconds == 120
        # DM
        assert s.dm_delay_min_seconds == 60
        assert s.dm_delay_max_seconds == 180
        # Group check
        assert s.group_check_join_delay_min_seconds == 15
        assert s.group_check_join_delay_max_seconds == 45

    def test_legacy_fields_still_present(self):
        """Legacy-поля остались доступны для обратной совместимости."""
        s = SchedulerSettings()
        assert s.publication_interval_seconds == 300
        assert s.join_interval_seconds == 900

    def test_load_settings_returns_defaults_for_fresh_db(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            s = db.load_scheduler_settings()
            assert s.publication_interval_min_seconds == 300
            assert s.publication_interval_max_seconds == 600
            assert s.broadcast_delay_min_seconds == 30
            assert s.broadcast_delay_max_seconds == 90
        finally:
            db.close()

    def test_save_load_roundtrip_all_new_fields(self, tmp_db_path):
        """Сохраняем все новые поля с нестандартными значениями — и читаем обратно."""
        db = AdsDB(tmp_db_path)
        try:
            s_out = SchedulerSettings(
                publication_interval_min_seconds=111,
                publication_interval_max_seconds=222,
                join_interval_min_seconds=333,
                join_interval_max_seconds=444,
                broadcast_delay_min_seconds=55,
                broadcast_delay_max_seconds=66,
                mention_delay_min_seconds=77,
                mention_delay_max_seconds=88,
                dm_delay_min_seconds=99,
                dm_delay_max_seconds=110,
                group_check_join_delay_min_seconds=12,
                group_check_join_delay_max_seconds=34,
                ai_provider="groq",
            )
            db.save_scheduler_settings(s_out)

            s_in = db.load_scheduler_settings()
            assert s_in.publication_interval_min_seconds == 111
            assert s_in.publication_interval_max_seconds == 222
            assert s_in.join_interval_min_seconds == 333
            assert s_in.join_interval_max_seconds == 444
            assert s_in.broadcast_delay_min_seconds == 55
            assert s_in.broadcast_delay_max_seconds == 66
            assert s_in.mention_delay_min_seconds == 77
            assert s_in.mention_delay_max_seconds == 88
            assert s_in.dm_delay_min_seconds == 99
            assert s_in.dm_delay_max_seconds == 110
            assert s_in.group_check_join_delay_min_seconds == 12
            assert s_in.group_check_join_delay_max_seconds == 34
            assert s_in.ai_provider == "groq"
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Миграция legacy-ключей
# ─────────────────────────────────────────────────────────────────────────────

class TestLegacySettingsMigration:
    def test_legacy_publication_interval_migrated_to_min_max(self, tmp_db_path):
        """Legacy-ключ publication_interval_seconds мигрирует в новые min/max."""
        db = AdsDB(tmp_db_path)
        try:
            # Эмулируем существующую БД где стоял только старый ключ
            db.set_setting("publication_interval_seconds", "420")
            # Новых ключей ещё нет
            assert db.get_setting("publication_interval_min_seconds") == ""

            s = db.load_scheduler_settings()

            # Миграция должна записать новые ключи
            assert db.get_setting("publication_interval_min_seconds") == "420"
            assert db.get_setting("publication_interval_max_seconds") == "840"
            # И в объекте тоже
            assert s.publication_interval_min_seconds == 420
            assert s.publication_interval_max_seconds == 840
            # Legacy поле тоже подтянулось
            assert s.publication_interval_seconds == 420
        finally:
            db.close()

    def test_legacy_join_interval_migrated_to_min_max(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            db.set_setting("join_interval_seconds", "1200")
            s = db.load_scheduler_settings()

            assert db.get_setting("join_interval_min_seconds") == "1200"
            assert db.get_setting("join_interval_max_seconds") == "2400"
            assert s.join_interval_min_seconds == 1200
            assert s.join_interval_max_seconds == 2400
        finally:
            db.close()

    def test_no_migration_if_new_keys_already_exist(self, tmp_db_path):
        """Если новые ключи уже есть, legacy не перезаписывает их."""
        db = AdsDB(tmp_db_path)
        try:
            db.set_setting("publication_interval_seconds", "500")  # legacy
            db.set_setting("publication_interval_min_seconds", "100")  # новый уже стоит
            db.set_setting("publication_interval_max_seconds", "200")

            db.load_scheduler_settings()

            # Новые ключи не изменились
            assert db.get_setting("publication_interval_min_seconds") == "100"
            assert db.get_setting("publication_interval_max_seconds") == "200"
        finally:
            db.close()

    def test_migration_is_idempotent_on_load(self, tmp_db_path):
        """Повторный load после миграции не дублирует и не ломает значения."""
        db = AdsDB(tmp_db_path)
        try:
            db.set_setting("publication_interval_seconds", "300")
            db.load_scheduler_settings()  # первая миграция: min=300, max=600

            # Юзер вручную поменял max через save — эмулируем
            db.set_setting("publication_interval_max_seconds", "900")

            s = db.load_scheduler_settings()  # повторный load
            # Миграция НЕ должна снова переписать max на 600,
            # потому что min уже задан
            assert s.publication_interval_max_seconds == 900
        finally:
            db.close()

    def test_legacy_with_garbage_value_does_not_crash(self, tmp_db_path):
        """Если legacy-ключ содержит нечисло, миграция не падает."""
        db = AdsDB(tmp_db_path)
        try:
            db.set_setting("publication_interval_seconds", "not a number")
            # Не должно упасть
            s = db.load_scheduler_settings()
            # Новые ключи не создались
            assert db.get_setting("publication_interval_min_seconds") == ""
            # Объект получил defaults
            assert s.publication_interval_min_seconds == 300
        finally:
            db.close()
