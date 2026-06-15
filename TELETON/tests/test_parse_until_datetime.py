"""Тесты парсера даты из текстов ошибок Telegram (ads_publisher)."""
from datetime import datetime, timedelta

from ads_publisher import _parse_until_datetime


class TestParseUntilDatetime:
    def test_returns_none_for_empty(self):
        assert _parse_until_datetime("") is None

    def test_returns_none_for_no_date(self):
        assert _parse_until_datetime("bla bla no date here") is None

    def test_parses_english_format_with_comma(self):
        """until 23.04.2099, 21:53"""
        result = _parse_until_datetime("you are restricted until 23.04.2099, 21:53")
        assert result is not None
        dt = datetime.fromisoformat(result)
        assert dt.year == 2099
        assert dt.month == 4
        assert dt.day == 23
        assert dt.hour == 21
        assert dt.minute == 53

    def test_parses_russian_format(self):
        """до 23.04.2099, 21:53"""
        result = _parse_until_datetime("вы ограничены до 23.04.2099, 21:53")
        assert result is not None
        dt = datetime.fromisoformat(result)
        assert dt.year == 2099

    def test_parses_without_comma(self):
        """until 23.04.2099 21:53"""
        result = _parse_until_datetime("until 23.04.2099 21:53")
        assert result is not None

    def test_parses_iso_format(self):
        """until 2099-04-23 21:53"""
        result = _parse_until_datetime("until 2099-04-23 21:53")
        assert result is not None
        dt = datetime.fromisoformat(result)
        assert dt.year == 2099

    def test_past_date_returns_none(self):
        """Дата в прошлом — ограничение уже снято, возвращаем None."""
        past_year = datetime.now().year - 1
        text = f"until 01.01.{past_year}, 10:00"
        assert _parse_until_datetime(text) is None

    def test_invalid_date_handled(self):
        """Невалидная дата (32.13.2099) — не падаем, возвращаем None."""
        result = _parse_until_datetime("until 32.13.2099, 21:53")
        assert result is None
