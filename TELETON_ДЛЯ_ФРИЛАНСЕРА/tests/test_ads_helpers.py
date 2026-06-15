"""
Тесты Этапа 3: публичные helper-функции для рандомных задержек рассылок.

Покрывают:
  - random_broadcast_delay_sec
  - random_mention_delay_sec
  - random_dm_delay_sec
  - random_group_check_delay_sec

Проверяем:
  - возвращаемое значение в пределах [min, max]
  - правильно читает соответствующие поля из settings
  - hard_min=1 (задержка не опускается ниже 1 секунды)
  - min==max → детерминированное значение
  - min>max (инверсия) → безопасная обработка
"""
import pytest

from ads_models import SchedulerSettings
from ads_scheduler import (
    random_broadcast_delay_sec,
    random_mention_delay_sec,
    random_dm_delay_sec,
    random_group_check_delay_sec,
)


# ─────────────────────────────────────────────────────────────────────────────
# random_broadcast_delay_sec
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomBroadcastDelay:
    def test_reads_broadcast_fields(self):
        """Использует именно broadcast_delay_min/max_seconds, а не другие поля."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=100,
            broadcast_delay_max_seconds=200,
            # Прочие поля с сильно отличающимися значениями —
            # если функция ошибочно возьмёт их, тест упадёт.
            mention_delay_min_seconds=1, mention_delay_max_seconds=2,
            dm_delay_min_seconds=1, dm_delay_max_seconds=2,
            group_check_join_delay_min_seconds=1, group_check_join_delay_max_seconds=2,
        )
        for _ in range(30):
            v = random_broadcast_delay_sec(s)
            assert 100 <= v <= 200

    def test_defaults_in_range(self):
        s = SchedulerSettings()  # broadcast: 30..90 по дефолту
        for _ in range(30):
            v = random_broadcast_delay_sec(s)
            assert 30 <= v <= 90

    def test_min_equals_max(self):
        s = SchedulerSettings(
            broadcast_delay_min_seconds=60,
            broadcast_delay_max_seconds=60,
        )
        assert random_broadcast_delay_sec(s) == 60.0

    def test_hard_min_one_second(self):
        """Даже при min=0 — не меньше 1 секунды."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=0,
            broadcast_delay_max_seconds=0,
        )
        v = random_broadcast_delay_sec(s)
        assert v >= 1

    def test_inverted_min_max_safe(self):
        """min > max — безопасно, возвращается min."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=90,
            broadcast_delay_max_seconds=30,
        )
        assert random_broadcast_delay_sec(s) == 90.0

    def test_returns_float(self):
        s = SchedulerSettings()
        v = random_broadcast_delay_sec(s)
        assert isinstance(v, float)


# ─────────────────────────────────────────────────────────────────────────────
# random_mention_delay_sec
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomMentionDelay:
    def test_reads_mention_fields(self):
        s = SchedulerSettings(
            mention_delay_min_seconds=500,
            mention_delay_max_seconds=700,
            broadcast_delay_min_seconds=1, broadcast_delay_max_seconds=2,
            dm_delay_min_seconds=1, dm_delay_max_seconds=2,
        )
        for _ in range(30):
            v = random_mention_delay_sec(s)
            assert 500 <= v <= 700

    def test_defaults_in_range(self):
        s = SchedulerSettings()  # mention: 45..120
        for _ in range(30):
            v = random_mention_delay_sec(s)
            assert 45 <= v <= 120

    def test_hard_min_one_second(self):
        s = SchedulerSettings(
            mention_delay_min_seconds=0,
            mention_delay_max_seconds=0,
        )
        assert random_mention_delay_sec(s) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# random_dm_delay_sec
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomDmDelay:
    def test_reads_dm_fields(self):
        s = SchedulerSettings(
            dm_delay_min_seconds=300,
            dm_delay_max_seconds=400,
            broadcast_delay_min_seconds=1, broadcast_delay_max_seconds=2,
            mention_delay_min_seconds=1, mention_delay_max_seconds=2,
        )
        for _ in range(30):
            v = random_dm_delay_sec(s)
            assert 300 <= v <= 400

    def test_defaults_in_range(self):
        s = SchedulerSettings()  # dm: 60..180
        for _ in range(30):
            v = random_dm_delay_sec(s)
            assert 60 <= v <= 180

    def test_hard_min_one_second(self):
        s = SchedulerSettings(
            dm_delay_min_seconds=0,
            dm_delay_max_seconds=0,
        )
        assert random_dm_delay_sec(s) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# random_group_check_delay_sec
# ─────────────────────────────────────────────────────────────────────────────

class TestRandomGroupCheckDelay:
    def test_reads_group_check_fields(self):
        s = SchedulerSettings(
            group_check_join_delay_min_seconds=25,
            group_check_join_delay_max_seconds=75,
            broadcast_delay_min_seconds=1, broadcast_delay_max_seconds=2,
            mention_delay_min_seconds=1, mention_delay_max_seconds=2,
            dm_delay_min_seconds=1, dm_delay_max_seconds=2,
        )
        for _ in range(30):
            v = random_group_check_delay_sec(s)
            assert 25 <= v <= 75

    def test_defaults_in_range(self):
        """Defaults заменяют старый hardcoded random.uniform(15, 45)."""
        s = SchedulerSettings()  # group_check: 15..45
        for _ in range(30):
            v = random_group_check_delay_sec(s)
            assert 15 <= v <= 45

    def test_hard_min_one_second(self):
        s = SchedulerSettings(
            group_check_join_delay_min_seconds=0,
            group_check_join_delay_max_seconds=0,
        )
        assert random_group_check_delay_sec(s) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Совокупно: все 4 функции независимы друг от друга
# ─────────────────────────────────────────────────────────────────────────────

class TestIndependence:
    def test_each_helper_uses_its_own_range(self):
        """Разные настройки для каждой подсистемы дают разные диапазоны."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=100, broadcast_delay_max_seconds=100,
            mention_delay_min_seconds=200, mention_delay_max_seconds=200,
            dm_delay_min_seconds=300, dm_delay_max_seconds=300,
            group_check_join_delay_min_seconds=50, group_check_join_delay_max_seconds=50,
        )
        assert random_broadcast_delay_sec(s) == 100.0
        assert random_mention_delay_sec(s) == 200.0
        assert random_dm_delay_sec(s) == 300.0
        assert random_group_check_delay_sec(s) == 50.0

    def test_randomness_across_calls(self):
        """Повторные вызовы дают разные значения (статистический тест)."""
        s = SchedulerSettings(
            broadcast_delay_min_seconds=1,
            broadcast_delay_max_seconds=1000,
        )
        values = {random_broadcast_delay_sec(s) for _ in range(50)}
        assert len(values) > 20  # шанс повторений очень мал при диапазоне 999
