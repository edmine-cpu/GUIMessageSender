"""
Тесты для фикса: при изменении interval_minutes / interval_minutes_max
в update_group() поле next_allowed_at автоматически обнуляется.

Раньше: юзер меняет интервал группы с 6ч на 1ч в GUI, но публикация
в эту группу не идёт ещё 5ч, потому что next_allowed_at был выставлен
под старое значение. Юзер думает «софт сломался».

Теперь: смена интервала → next_allowed_at очищается → новые настройки
применяются немедленно.
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from ads_database import AdsDB
from ads_models import GroupTarget, GROUP_STATUS_ACTIVE


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    database = AdsDB(path)
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def _make_group(interval_minutes=60, interval_minutes_max=0,
                next_allowed_at="", notes="initial"):
    return GroupTarget(
        link="https://t.me/testgroup",
        title="Test group",
        category="general",
        interval_minutes=interval_minutes,
        interval_minutes_max=interval_minutes_max,
        hours_start=0,
        hours_end=23,
        notes=notes,
        status=GROUP_STATUS_ACTIVE,
        next_allowed_at=next_allowed_at,
    )


class TestUpdateGroupResetsNextAllowed:
    def test_resets_when_interval_min_changes(self, db):
        """Меняем interval_minutes — next_allowed_at должен обнулиться."""
        group = _make_group(interval_minutes=60)
        db.add_group(group)
        saved = db.get_all_groups()[0]

        future = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
        db.set_group_next_allowed_at(saved.id, future)

        saved.interval_minutes = 30
        saved.next_allowed_at = future
        db.update_group(saved)

        updated = db.get_all_groups()[0]
        assert updated.next_allowed_at == ""
        assert updated.interval_minutes == 30

    def test_resets_when_interval_max_changes(self, db):
        group = _make_group(interval_minutes=60, interval_minutes_max=120)
        db.add_group(group)
        saved = db.get_all_groups()[0]

        future = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
        db.set_group_next_allowed_at(saved.id, future)

        saved.interval_minutes_max = 90
        saved.next_allowed_at = future
        db.update_group(saved)

        updated = db.get_all_groups()[0]
        assert updated.next_allowed_at == ""
        assert updated.interval_minutes_max == 90

    def test_keeps_next_allowed_when_interval_unchanged(self, db):
        group = _make_group(interval_minutes=60, notes="initial")
        db.add_group(group)
        saved = db.get_all_groups()[0]

        future = (datetime.now() + timedelta(hours=5)).isoformat(timespec="seconds")
        db.set_group_next_allowed_at(saved.id, future)
        saved = db.get_all_groups()[0]
        assert saved.next_allowed_at == future

        saved.notes = "updated comment"
        db.update_group(saved)

        updated = db.get_all_groups()[0]
        assert updated.next_allowed_at == future
        assert updated.notes == "updated comment"

    def test_keeps_when_both_fields_unchanged_explicitly(self, db):
        group = _make_group(interval_minutes=60, interval_minutes_max=120)
        db.add_group(group)
        saved = db.get_all_groups()[0]

        future = (datetime.now() + timedelta(hours=3)).isoformat(timespec="seconds")
        db.set_group_next_allowed_at(saved.id, future)
        saved = db.get_all_groups()[0]

        saved.status = "paused"
        db.update_group(saved)

        updated = db.get_all_groups()[0]
        assert updated.next_allowed_at == future
        assert updated.status == "paused"

    def test_resets_with_empty_next_allowed_at(self, db):
        group = _make_group(interval_minutes=60)
        db.add_group(group)
        saved = db.get_all_groups()[0]
        assert saved.next_allowed_at == ""

        saved.interval_minutes = 30
        db.update_group(saved)

        updated = db.get_all_groups()[0]
        assert updated.next_allowed_at == ""
        assert updated.interval_minutes == 30
