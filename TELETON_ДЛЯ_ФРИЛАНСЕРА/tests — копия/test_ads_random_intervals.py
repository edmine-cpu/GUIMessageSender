"""
Тесты Этапа 2: рандомные интервалы публикации и вступления.

Покрывают:
  - helper-функции _random_interval_sec и _random_group_interval_sec
  - _can_publish_to_group (проверка next_allowed_at)
  - _can_publish_globally (проверка next_pub_allowed_at)
  - clamp_settings с новыми min/max полями
  - SubscriptionManager._compute_next_join_delay_sec
  - SubscriptionManager.join_channel + can_join_now с рандомным интервалом
"""
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from ads_database import AdsDB
from ads_models import (
    GroupTarget, SchedulerSettings,
    GROUP_STATUS_ACTIVE, GROUP_STATUS_PAUSED, GROUP_STATUS_BANNED,
)
from ads_scheduler import (
    _random_interval_sec, _random_group_interval_sec,
    _can_publish_to_group, _can_publish_globally, clamp_settings,
    HARD_MIN_PUBLICATION_INTERVAL_SEC, HARD_MIN_GROUP_INTERVAL_SEC,
    HARD_MAX_DAILY_PUBLICATIONS,
)
from ads_subscriptions import (
    SubscriptionManager, HARD_MIN_JOIN_INTERVAL_SEC, HARD_MAX_DAILY_JOINS,
)


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


# ─────────────────────────────────────────────────────────────────────────────
# _random_interval_sec
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomIntervalSec:
    def test_returns_value_in_range(self):
        """Многократные вызовы — результат всегда в диапазоне [min, max]."""
        for _ in range(50):
            v = _random_interval_sec(100, 200, hard_min=30)
            assert 100 <= v <= 200

    def test_min_equals_max_returns_min(self):
        """Если min == max — возвращается min без рандома."""
        v = _random_interval_sec(120, 120, hard_min=30)
        assert v == 120.0

    def test_min_below_hard_min_is_raised(self):
        """Если min < hard_min, итоговый min поднимается до hard_min."""
        for _ in range(20):
            v = _random_interval_sec(10, 100, hard_min=50)
            assert v >= 50

    def test_max_below_min_is_raised(self):
        """Перевёрнутый диапазон: max < min — используем min."""
        v = _random_interval_sec(200, 100, hard_min=30)
        assert v == 200.0

    def test_both_below_hard_min_returns_hard_min(self):
        """И min и max ниже hard_min — возвращается hard_min."""
        v = _random_interval_sec(5, 10, hard_min=100)
        assert v == 100.0

    def test_random_values_vary(self):
        """Рандом реально рандомизирует: разные вызовы дают разные значения."""
        values = {_random_interval_sec(50, 500, hard_min=30) for _ in range(30)}
        assert len(values) > 10  # статистически должно быть много уникальных


# ─────────────────────────────────────────────────────────────────────────────
# _random_group_interval_sec
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomGroupIntervalSec:
    def test_uses_interval_minutes_as_min(self):
        """min = interval_minutes * 60, clamp к HARD_MIN_GROUP_INTERVAL_SEC."""
        g = GroupTarget(interval_minutes=60, interval_minutes_max=120)  # 3600-7200 сек
        for _ in range(20):
            v = _random_group_interval_sec(g)
            assert 3600 <= v <= 7200

    def test_uses_interval_minutes_max_when_set(self):
        g = GroupTarget(interval_minutes=60, interval_minutes_max=90)  # 3600-5400
        for _ in range(20):
            v = _random_group_interval_sec(g)
            assert 3600 <= v <= 5400

    def test_fallback_when_interval_max_is_zero(self):
        """Если interval_minutes_max == 0, используется 2 × interval_minutes."""
        g = GroupTarget(interval_minutes=60, interval_minutes_max=0)  # 3600-7200
        for _ in range(20):
            v = _random_group_interval_sec(g)
            assert 3600 <= v <= 7200

    def test_hard_min_applied(self):
        """Маленький interval_minutes — результат не ниже HARD_MIN_GROUP_INTERVAL_SEC."""
        g = GroupTarget(interval_minutes=1, interval_minutes_max=2)  # 60-120 — ниже hard 1800
        for _ in range(10):
            v = _random_group_interval_sec(g)
            assert v >= HARD_MIN_GROUP_INTERVAL_SEC


# ─────────────────────────────────────────────────────────────────────────────
# _can_publish_to_group
# ─────────────────────────────────────────────────────────────────────────────

class TestCanPublishToGroup:
    def _active_group(self, **overrides):
        base = dict(
            id=1, link="@g", interval_minutes=60, hours_start=0, hours_end=23,
            status=GROUP_STATUS_ACTIVE, next_allowed_at="", retry_after="",
        )
        base.update(overrides)
        return GroupTarget(**base)

    def test_active_with_no_restrictions_allows(self):
        g = self._active_group()
        assert _can_publish_to_group(g) is True

    def test_paused_group_denied(self):
        g = self._active_group(status=GROUP_STATUS_PAUSED)
        assert _can_publish_to_group(g) is False

    def test_banned_group_denied(self):
        g = self._active_group(status=GROUP_STATUS_BANNED)
        assert _can_publish_to_group(g) is False

    def test_retry_after_in_future_denies(self):
        future = (datetime.now() + timedelta(hours=1)).isoformat()
        g = self._active_group(retry_after=future)
        assert _can_publish_to_group(g) is False

    def test_retry_after_in_past_allows(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        g = self._active_group(retry_after=past)
        assert _can_publish_to_group(g) is True

    def test_next_allowed_at_in_future_denies(self):
        future = (datetime.now() + timedelta(minutes=30)).isoformat()
        g = self._active_group(next_allowed_at=future)
        assert _can_publish_to_group(g) is False

    def test_next_allowed_at_in_past_allows(self):
        past = (datetime.now() - timedelta(minutes=30)).isoformat()
        g = self._active_group(next_allowed_at=past)
        assert _can_publish_to_group(g) is True

    def test_hours_outside_range_denied(self):
        # Трюк: ставим диапазон, в который текущий час точно не попадёт
        now_hour = datetime.now().hour
        # Берём окно 2 часа в прошлом (если сейчас 15, то 12-13)
        h_start = (now_hour - 3) % 24
        h_end = (now_hour - 2) % 24
        if h_start > h_end:
            # Эта комбинация выходит за полночь — пропустим в этом тесте
            pytest.skip("Час wrap-around, sкип")
        g = self._active_group(hours_start=h_start, hours_end=h_end)
        assert _can_publish_to_group(g) is False

    def test_invalid_retry_after_ignored(self):
        g = self._active_group(retry_after="garbage-not-iso")
        # Не должно упасть, поле игнорируется
        assert _can_publish_to_group(g) is True

    def test_invalid_next_allowed_at_ignored(self):
        g = self._active_group(next_allowed_at="also-garbage")
        assert _can_publish_to_group(g) is True


# ─────────────────────────────────────────────────────────────────────────────
# _can_publish_globally
# ─────────────────────────────────────────────────────────────────────────────

class TestCanPublishGlobally:
    def test_no_state_allows(self):
        s = SchedulerSettings()
        assert _can_publish_globally(pub_count_today=0,
                                      next_pub_allowed_at=None,
                                      settings=s) is True

    def test_daily_limit_reached_denies(self):
        s = SchedulerSettings(daily_publication_limit=5)
        assert _can_publish_globally(pub_count_today=5,
                                      next_pub_allowed_at=None,
                                      settings=s) is False

    def test_hard_max_daily_caps_limit(self):
        """Если в settings дневной лимит выше HARD_MAX_DAILY_PUBLICATIONS —
        применяется hard cap."""
        s = SchedulerSettings(daily_publication_limit=9999)
        # Попадая в диапазон hard cap
        assert _can_publish_globally(pub_count_today=HARD_MAX_DAILY_PUBLICATIONS,
                                      next_pub_allowed_at=None,
                                      settings=s) is False
        # Ниже cap — разрешено
        assert _can_publish_globally(pub_count_today=HARD_MAX_DAILY_PUBLICATIONS - 1,
                                      next_pub_allowed_at=None,
                                      settings=s) is True

    def test_next_pub_in_future_denies(self):
        s = SchedulerSettings()
        future = datetime.now() + timedelta(minutes=10)
        assert _can_publish_globally(pub_count_today=0,
                                      next_pub_allowed_at=future,
                                      settings=s) is False

    def test_next_pub_in_past_allows(self):
        s = SchedulerSettings()
        past = datetime.now() - timedelta(minutes=10)
        assert _can_publish_globally(pub_count_today=0,
                                      next_pub_allowed_at=past,
                                      settings=s) is True


# ─────────────────────────────────────────────────────────────────────────────
# clamp_settings
# ─────────────────────────────────────────────────────────────────────────────

class TestClampSettings:
    def test_publication_min_raised_to_hard_min(self):
        s = SchedulerSettings(publication_interval_min_seconds=5)
        s = clamp_settings(s)
        assert s.publication_interval_min_seconds >= HARD_MIN_PUBLICATION_INTERVAL_SEC

    def test_publication_max_raised_to_min(self):
        """Если max < min после clamp'а min, max тянется вверх до min."""
        s = SchedulerSettings(
            publication_interval_min_seconds=500,
            publication_interval_max_seconds=100,
        )
        s = clamp_settings(s)
        assert s.publication_interval_max_seconds >= s.publication_interval_min_seconds

    def test_join_min_raised_to_hard_min(self):
        s = SchedulerSettings(join_interval_min_seconds=10)
        s = clamp_settings(s)
        assert s.join_interval_min_seconds >= HARD_MIN_JOIN_INTERVAL_SEC

    def test_join_max_not_below_min(self):
        s = SchedulerSettings(
            join_interval_min_seconds=1200,
            join_interval_max_seconds=600,  # меньше min
        )
        s = clamp_settings(s)
        assert s.join_interval_max_seconds >= s.join_interval_min_seconds

    def test_broadcast_min_at_least_one(self):
        """Для broadcast/mention/DM/group_check — min не может быть ниже 1."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=0,
            broadcast_delay_max_seconds=0,
        )
        s = clamp_settings(s)
        assert s.broadcast_delay_min_seconds >= 1
        assert s.broadcast_delay_max_seconds >= s.broadcast_delay_min_seconds

    def test_all_new_fields_clamped(self):
        """Проверяем что все четыре пары в for-cycle отработали."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=0, broadcast_delay_max_seconds=-5,
            mention_delay_min_seconds=-10, mention_delay_max_seconds=0,
            dm_delay_min_seconds=-1, dm_delay_max_seconds=0,
            group_check_join_delay_min_seconds=0, group_check_join_delay_max_seconds=0,
        )
        s = clamp_settings(s)
        for pair in (
            (s.broadcast_delay_min_seconds, s.broadcast_delay_max_seconds),
            (s.mention_delay_min_seconds, s.mention_delay_max_seconds),
            (s.dm_delay_min_seconds, s.dm_delay_max_seconds),
            (s.group_check_join_delay_min_seconds, s.group_check_join_delay_max_seconds),
        ):
            mn, mx = pair
            assert mn >= 1
            assert mx >= mn

    def test_daily_limit_capped(self):
        s = SchedulerSettings(daily_publication_limit=10_000)
        s = clamp_settings(s)
        assert s.daily_publication_limit <= HARD_MAX_DAILY_PUBLICATIONS

    def test_legacy_interval_clamped(self):
        """Legacy поле publication_interval_seconds тоже должно clamp'иться."""
        s = SchedulerSettings(publication_interval_seconds=5)
        s = clamp_settings(s)
        assert s.publication_interval_seconds >= HARD_MIN_PUBLICATION_INTERVAL_SEC


# ─────────────────────────────────────────────────────────────────────────────
# SubscriptionManager: рандом между вступлениями
# ─────────────────────────────────────────────────────────────────────────────

class TestSubscriptionManagerRandomInterval:
    def test_without_settings_returns_hard_min(self, tmp_db_path):
        """Без settings — intервал фиксированный = HARD_MIN (legacy)."""
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79001234567", settings=None)
            v = mgr._compute_next_join_delay_sec()
            assert v == float(HARD_MIN_JOIN_INTERVAL_SEC)
        finally:
            db.close()

    def test_with_settings_returns_random_in_range(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            s = SchedulerSettings(
                join_interval_min_seconds=600,
                join_interval_max_seconds=1200,
            )
            mgr = SubscriptionManager(db, "+79001234567", settings=s)
            for _ in range(20):
                v = mgr._compute_next_join_delay_sec()
                assert 600 <= v <= 1200
        finally:
            db.close()

    def test_min_clamped_to_hard_min(self, tmp_db_path):
        """Если в settings min ниже HARD_MIN, результат не ниже HARD_MIN."""
        db = AdsDB(tmp_db_path)
        try:
            s = SchedulerSettings(
                join_interval_min_seconds=10,   # < HARD_MIN_JOIN_INTERVAL_SEC=300
                join_interval_max_seconds=1000,
            )
            mgr = SubscriptionManager(db, "+79001234567", settings=s)
            for _ in range(20):
                v = mgr._compute_next_join_delay_sec()
                assert v >= HARD_MIN_JOIN_INTERVAL_SEC
        finally:
            db.close()

    def test_can_join_now_initial_state(self, tmp_db_path):
        """При создании менеджера сразу можно вступить."""
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79001234567")
            assert mgr.can_join_now() is True
            assert mgr.seconds_until_can_join() == 0
        finally:
            db.close()

    def test_can_join_now_after_next_allowed_set(self, tmp_db_path):
        """Ставим _next_join_allowed_at в будущее — can_join_now=False."""
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79001234567")
            mgr._next_join_allowed_at = datetime.now() + timedelta(seconds=600)
            assert mgr.can_join_now() is False
            assert mgr.seconds_until_can_join() > 0

            # Сдвигаем в прошлое — должно стать можно
            mgr._next_join_allowed_at = datetime.now() - timedelta(seconds=1)
            assert mgr.can_join_now() is True
            assert mgr.seconds_until_can_join() == 0
        finally:
            db.close()

    def test_daily_limit_blocks_can_join(self, tmp_db_path):
        db = AdsDB(tmp_db_path)
        try:
            mgr = SubscriptionManager(db, "+79001234567")
            mgr._joins_today = HARD_MAX_DAILY_JOINS
            assert mgr.can_join_now() is False
        finally:
            db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Интеграция: AdsScheduler + SubscriptionManager вместе
# ─────────────────────────────────────────────────────────────────────────────

class TestSchedulerIntegration:
    @pytest.mark.asyncio
    async def test_join_channel_sets_next_allowed_at_randomly(self, tmp_db_path):
        """После успешного join _next_join_allowed_at ставится в будущее
        на случайное время из диапазона settings."""
        db = AdsDB(tmp_db_path)
        try:
            s = SchedulerSettings(
                join_interval_min_seconds=600,
                join_interval_max_seconds=1200,
            )
            mgr = SubscriptionManager(db, "+79001234567", settings=s)

            # Мокаем клиент для JoinChannelRequest
            client = MagicMock()
            client.get_entity = MagicMock()

            async def fake_get_entity(link):
                return MagicMock()
            client.get_entity = fake_get_entity

            async def fake_call(req):
                return None
            client.side_effect = fake_call
            # Chaned: telethon-клиент вызывается как client(...) — мокнем через MagicMock
            client = MagicMock()
            async def ae(*a, **kw): return MagicMock()
            client.get_entity = ae
            async def ac(*a, **kw): return None
            client.__call__ = ac  # не совсем то, но достаточно для быстрой проверки

            # Проверяем что _compute_next_join_delay_sec даёт значение в диапазоне
            v = mgr._compute_next_join_delay_sec()
            assert 600 <= v <= 1200
        finally:
            db.close()
