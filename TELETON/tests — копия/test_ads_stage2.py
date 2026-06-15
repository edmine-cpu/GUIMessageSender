"""
Тесты Этапа 2: рандомные интервалы в планировщике и менеджере подписок.
  - _random_interval_sec / _random_group_interval_sec (edge cases)
  - _can_publish_to_group / _can_publish_globally с next_allowed_at
  - clamp_settings для новых min/max полей
  - SubscriptionManager с рандомным интервалом join
"""
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from ads_models import (
    GroupTarget, SchedulerSettings,
    GROUP_STATUS_ACTIVE, GROUP_STATUS_PAUSED,
)
from ads_scheduler import (
    _random_interval_sec, _random_group_interval_sec,
    _can_publish_to_group, _can_publish_globally,
    clamp_settings,
    HARD_MIN_PUBLICATION_INTERVAL_SEC, HARD_MIN_GROUP_INTERVAL_SEC,
    HARD_MAX_DAILY_PUBLICATIONS,
)
from ads_subscriptions import (
    SubscriptionManager, HARD_MIN_JOIN_INTERVAL_SEC, HARD_MAX_DAILY_JOINS,
)


# ─────────────────────────────────────────────────────────────────────────────
# _random_interval_sec: генерация случайного интервала с hard_min
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomIntervalSec:
    def test_value_in_range(self):
        """За 1000 итераций ни разу не вылетаем за [min, max]."""
        for _ in range(1000):
            v = _random_interval_sec(100, 200, 30)
            assert 100 <= v <= 200

    def test_min_equal_max_returns_min(self):
        v = _random_interval_sec(150, 150, 30)
        assert v == 150.0

    def test_hard_min_clamps_lo(self):
        """Если min < hard_min, результат >= hard_min."""
        for _ in range(100):
            v = _random_interval_sec(10, 50, 100)
            assert v >= 100

    def test_max_below_min_uses_min(self):
        """Если max < min, считаем что max = min (не ломаемся)."""
        for _ in range(100):
            v = _random_interval_sec(200, 100, 30)
            # После clamp lo=200, hi = max(100, 200) = 200
            assert v == 200.0

    def test_both_below_hard_min(self):
        v = _random_interval_sec(10, 20, 100)
        assert v == 100.0


class TestRandomGroupIntervalSec:
    def test_uses_interval_minutes_as_min(self):
        g = GroupTarget(interval_minutes=60, interval_minutes_max=120)
        for _ in range(100):
            v = _random_group_interval_sec(g)
            # Должно быть в [60*60, 120*60] — но не ниже HARD_MIN_GROUP_INTERVAL_SEC (1800)
            assert v >= HARD_MIN_GROUP_INTERVAL_SEC
            assert v >= 60 * 60
            assert v <= 120 * 60

    def test_zero_max_means_2x_min(self):
        """Если interval_minutes_max=0, max = interval_minutes × 2."""
        g = GroupTarget(interval_minutes=60, interval_minutes_max=0)
        for _ in range(100):
            v = _random_group_interval_sec(g)
            assert 60 * 60 <= v <= 120 * 60

    def test_hard_min_applied(self):
        """Даже если interval_minutes мал, снизу держит HARD_MIN_GROUP_INTERVAL_SEC."""
        g = GroupTarget(interval_minutes=5, interval_minutes_max=10)
        v = _random_group_interval_sec(g)
        assert v >= HARD_MIN_GROUP_INTERVAL_SEC


# ─────────────────────────────────────────────────────────────────────────────
# _can_publish_to_group: проверки с next_allowed_at
# ─────────────────────────────────────────────────────────────────────────────

class TestCanPublishToGroup:
    def _group(self, **overrides):
        """Шаблон активной группы, разрешено сейчас."""
        defaults = dict(
            status=GROUP_STATUS_ACTIVE,
            hours_start=0, hours_end=23,
            retry_after="", next_allowed_at="",
        )
        defaults.update(overrides)
        return GroupTarget(**defaults)

    def test_active_group_without_limits_can_publish(self):
        g = self._group()
        assert _can_publish_to_group(g) is True

    def test_paused_group_cannot_publish(self):
        g = self._group(status=GROUP_STATUS_PAUSED)
        assert _can_publish_to_group(g) is False

    def test_retry_after_in_future_blocks(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        g = self._group(retry_after=future)
        assert _can_publish_to_group(g) is False

    def test_retry_after_in_past_does_not_block(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        g = self._group(retry_after=past)
        assert _can_publish_to_group(g) is True

    def test_next_allowed_at_in_future_blocks(self):
        future = (datetime.now() + timedelta(minutes=30)).isoformat()
        g = self._group(next_allowed_at=future)
        assert _can_publish_to_group(g) is False

    def test_next_allowed_at_in_past_does_not_block(self):
        past = (datetime.now() - timedelta(minutes=30)).isoformat()
        g = self._group(next_allowed_at=past)
        assert _can_publish_to_group(g) is True

    def test_outside_hours_blocks(self):
        """Если текущий час вне hours_start..hours_end — нельзя."""
        # Берём диапазон, который точно не содержит текущий час
        current = datetime.now().hour
        # Делаем дырку вокруг текущего часа
        if current >= 2:
            hs, he = 0, current - 1
        else:
            hs, he = current + 1, 23
        g = self._group(hours_start=hs, hours_end=he)
        assert _can_publish_to_group(g) is False

    def test_invalid_retry_after_ignored(self):
        """Мусорная строка в retry_after не должна вешаться."""
        g = self._group(retry_after="garbage-not-iso")
        assert _can_publish_to_group(g) is True

    def test_invalid_next_allowed_at_ignored(self):
        g = self._group(next_allowed_at="garbage")
        assert _can_publish_to_group(g) is True


# ─────────────────────────────────────────────────────────────────────────────
# _can_publish_globally
# ─────────────────────────────────────────────────────────────────────────────

class TestCanPublishGlobally:
    def test_no_history_allows_publish(self):
        s = SchedulerSettings()
        assert _can_publish_globally(0, None, s) is True

    def test_daily_limit_blocks(self):
        s = SchedulerSettings(daily_publication_limit=5)
        assert _can_publish_globally(5, None, s) is False
        assert _can_publish_globally(100, None, s) is False

    def test_daily_limit_clamped_to_hard_max(self):
        """Если в settings лимит > hard_max, применяется hard_max."""
        s = SchedulerSettings(daily_publication_limit=1000)
        # hard_max = HARD_MAX_DAILY_PUBLICATIONS = 50
        assert _can_publish_globally(HARD_MAX_DAILY_PUBLICATIONS, None, s) is False
        assert _can_publish_globally(HARD_MAX_DAILY_PUBLICATIONS - 1, None, s) is True

    def test_next_allowed_at_in_future_blocks(self):
        s = SchedulerSettings()
        future = datetime.now() + timedelta(minutes=5)
        assert _can_publish_globally(0, future, s) is False

    def test_next_allowed_at_in_past_allows(self):
        s = SchedulerSettings()
        past = datetime.now() - timedelta(minutes=5)
        assert _can_publish_globally(0, past, s) is True


# ─────────────────────────────────────────────────────────────────────────────
# clamp_settings
# ─────────────────────────────────────────────────────────────────────────────

class TestClampSettings:
    def test_publication_min_raised_to_hard_min(self):
        s = SchedulerSettings(publication_interval_min_seconds=5)
        clamp_settings(s)
        assert s.publication_interval_min_seconds == HARD_MIN_PUBLICATION_INTERVAL_SEC

    def test_publication_max_raised_to_new_min_if_below(self):
        s = SchedulerSettings(
            publication_interval_min_seconds=500,
            publication_interval_max_seconds=100,  # < min
        )
        clamp_settings(s)
        assert s.publication_interval_min_seconds == 500
        assert s.publication_interval_max_seconds == 500  # подтянулся к min

    def test_join_min_raised_to_hard_min(self):
        s = SchedulerSettings(join_interval_min_seconds=10)
        clamp_settings(s)
        assert s.join_interval_min_seconds == HARD_MIN_JOIN_INTERVAL_SEC

    def test_broadcast_min_at_least_1(self):
        s = SchedulerSettings(broadcast_delay_min_seconds=0)
        clamp_settings(s)
        assert s.broadcast_delay_min_seconds == 1

    def test_dm_max_raised_to_min(self):
        s = SchedulerSettings(
            dm_delay_min_seconds=100,
            dm_delay_max_seconds=50,
        )
        clamp_settings(s)
        assert s.dm_delay_max_seconds == 100

    def test_valid_values_pass_through(self):
        s = SchedulerSettings(
            publication_interval_min_seconds=300,
            publication_interval_max_seconds=600,
            broadcast_delay_min_seconds=30,
            broadcast_delay_max_seconds=90,
        )
        clamp_settings(s)
        assert s.publication_interval_min_seconds == 300
        assert s.publication_interval_max_seconds == 600
        assert s.broadcast_delay_min_seconds == 30
        assert s.broadcast_delay_max_seconds == 90

    def test_daily_limit_clamped_to_hard_max(self):
        s = SchedulerSettings(daily_publication_limit=999)
        clamp_settings(s)
        assert s.daily_publication_limit == HARD_MAX_DAILY_PUBLICATIONS


# ─────────────────────────────────────────────────────────────────────────────
# SubscriptionManager: рандомные интервалы join + accessibility
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    yield path
    for suffix in ("", "-wal", "-shm", "-journal"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


class TestSubscriptionManagerRandomInterval:
    def test_compute_delay_without_settings_uses_hard_min(self, tmp_db_path):
        """Без settings — фиксированный hard_min (обратная совместимость)."""
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            assert mgr._compute_next_join_delay_sec() == float(HARD_MIN_JOIN_INTERVAL_SEC)
        finally:
            db.close()

    def test_compute_delay_with_settings_in_range(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            s = SchedulerSettings(
                join_interval_min_seconds=900,
                join_interval_max_seconds=1800,
            )
            mgr = SubscriptionManager(db, "+79991234567", settings=s)
            for _ in range(500):
                v = mgr._compute_next_join_delay_sec()
                assert 900 <= v <= 1800
        finally:
            db.close()

    def test_compute_delay_respects_hard_min(self, tmp_db_path):
        """Даже если в settings min < hard_min, возвращается значение >= hard_min."""
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            s = SchedulerSettings(
                join_interval_min_seconds=10,
                join_interval_max_seconds=50,
            )
            mgr = SubscriptionManager(db, "+79991234567", settings=s)
            v = mgr._compute_next_join_delay_sec()
            assert v >= HARD_MIN_JOIN_INTERVAL_SEC
        finally:
            db.close()

    def test_can_join_now_true_initially(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            assert mgr.can_join_now() is True
        finally:
            db.close()

    def test_can_join_now_false_after_recent_join(self, tmp_db_path):
        """После установки _next_join_allowed_at в будущее — False."""
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            mgr._next_join_allowed_at = datetime.now() + timedelta(minutes=5)
            assert mgr.can_join_now() is False
        finally:
            db.close()

    def test_can_join_now_true_when_next_allowed_past(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            mgr._next_join_allowed_at = datetime.now() - timedelta(minutes=1)
            assert mgr.can_join_now() is True
        finally:
            db.close()

    def test_daily_limit_blocks(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            mgr._joins_today = HARD_MAX_DAILY_JOINS
            assert mgr.can_join_now() is False
        finally:
            db.close()

    def test_seconds_until_can_join_zero_initially(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            assert mgr.seconds_until_can_join() == 0
        finally:
            db.close()

    def test_seconds_until_can_join_counts_down(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79991234567")
            mgr._next_join_allowed_at = datetime.now() + timedelta(seconds=120)
            remaining = mgr.seconds_until_can_join()
            assert 115 <= remaining <= 120
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Интеграция: set_group_next_allowed_at + _can_publish_to_group
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerIntegration:
    def test_group_becomes_unavailable_after_setting_future_next_allowed(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            gid = db.add_group(GroupTarget(
                link="@test_group",
                title="Test",
                hours_start=0, hours_end=23,
            ))
            # Изначально можно публиковать
            g = db.get_group(gid)
            assert _can_publish_to_group(g) is True

            # Ставим next_allowed в будущее
            future = (datetime.now() + timedelta(hours=1)).isoformat()
            db.set_group_next_allowed_at(gid, future)

            g = db.get_group(gid)
            assert _can_publish_to_group(g) is False
        finally:
            db.close()

    def test_group_available_again_after_time_passes(self, tmp_db_path):
        from ads_database import AdsDB
        db = AdsDB(tmp_db_path)
        try:
            gid = db.add_group(GroupTarget(
                link="@test_group2",
                title="Test2",
                hours_start=0, hours_end=23,
            ))
            past = (datetime.now() - timedelta(hours=1)).isoformat()
            db.set_group_next_allowed_at(gid, past)

            g = db.get_group(gid)
            assert _can_publish_to_group(g) is True
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Детерминированный тест с фиксированным random.uniform
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterministicRandom:
    def test_random_interval_uses_uniform(self):
        """Убеждаемся что random.uniform действительно вызывается."""
        with patch("ads_scheduler.random.uniform", return_value=42.0) as mock:
            v = _random_interval_sec(100, 200, 30)
            assert v == 42.0
            mock.assert_called_once_with(100, 200)

    def test_random_interval_no_uniform_when_min_equals_max(self):
        """Не вызываем uniform если min==max (мелкая оптимизация)."""
        with patch("ads_scheduler.random.uniform") as mock:
            v = _random_interval_sec(100, 100, 30)
            assert v == 100.0
            mock.assert_not_called()
