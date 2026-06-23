"""
Тесты статус-машины аккаунтов:
  - on_connect_success сбрасывает счётчик и возвращает из network_issue
  - on_connect_network_issue после CONNECT_FAIL_THRESHOLD помечает network_issue
  - on_connect_error после CONNECT_FAIL_THRESHOLD помечает needs_reauth
  - get_active_accounts фильтрует по status и flood_until
  - set_account_flood_until ставит паузу
"""
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from database import Database, CONNECT_FAIL_THRESHOLD, NETWORK_RECOVERY_MINUTES
from models import (
    Account,
    ACCOUNT_STATUS_ACTIVE, ACCOUNT_STATUS_NEEDS_REAUTH,
    ACCOUNT_STATUS_BANNED, ACCOUNT_STATUS_NETWORK_ISSUE,
)


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    database = Database(path)
    yield database
    database.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def _make_account(phone="+79001234567", status=ACCOUNT_STATUS_ACTIVE,
                  is_active=True, fail_count=0, flood_until="",
                  api_id=2040):
    return Account(
        phone=phone,
        session_name=f"data/sessions/session_{phone}",
        is_active=is_active,
        status=status,
        connect_fail_count=fail_count,
        flood_until=flood_until,
        api_id=api_id,
        api_hash="test_hash",
    )


class TestConnectSuccess:
    def test_resets_fail_counter(self, db):
        acc = _make_account(fail_count=2)
        db.add_account(acc)

        db.on_connect_success(acc.phone)

        restored = db.get_all_accounts()[0]
        assert restored.connect_fail_count == 0

    def test_returns_network_issue_to_active(self, db):
        acc = _make_account(status=ACCOUNT_STATUS_NETWORK_ISSUE, fail_count=3,
                             flood_until="2099-01-01T00:00:00")
        db.add_account(acc)

        db.on_connect_success(acc.phone)

        restored = db.get_all_accounts()[0]
        assert restored.status == ACCOUNT_STATUS_ACTIVE
        assert restored.flood_until == ""

    def test_does_not_revive_banned(self, db):
        """banned не должен сбрасываться через on_connect_success."""
        acc = _make_account(status=ACCOUNT_STATUS_BANNED, fail_count=3)
        db.add_account(acc)

        db.on_connect_success(acc.phone)

        restored = db.get_all_accounts()[0]
        assert restored.status == ACCOUNT_STATUS_BANNED

    def test_does_not_revive_needs_reauth(self, db):
        acc = _make_account(status=ACCOUNT_STATUS_NEEDS_REAUTH, fail_count=3)
        db.add_account(acc)

        db.on_connect_success(acc.phone)

        restored = db.get_all_accounts()[0]
        assert restored.status == ACCOUNT_STATUS_NEEDS_REAUTH


class TestConnectNetworkIssue:
    def test_increments_counter(self, db):
        acc = _make_account(fail_count=0)
        db.add_account(acc)

        db.on_connect_network_issue(acc.phone, "timeout")

        restored = db.get_all_accounts()[0]
        assert restored.connect_fail_count == 1
        assert restored.status == ACCOUNT_STATUS_ACTIVE  # ещё под порогом

    def test_flags_network_issue_at_threshold(self, db):
        acc = _make_account(fail_count=CONNECT_FAIL_THRESHOLD - 1)
        db.add_account(acc)

        db.on_connect_network_issue(acc.phone, "timeout")

        restored = db.get_all_accounts()[0]
        assert restored.connect_fail_count == CONNECT_FAIL_THRESHOLD
        assert restored.status == ACCOUNT_STATUS_NETWORK_ISSUE
        assert restored.flood_until != ""
        # cooldown должен быть в будущем, ~NETWORK_RECOVERY_MINUTES минут
        until = datetime.fromisoformat(restored.flood_until)
        expected_min = datetime.now() + timedelta(minutes=NETWORK_RECOVERY_MINUTES - 1)
        expected_max = datetime.now() + timedelta(minutes=NETWORK_RECOVERY_MINUTES + 1)
        assert expected_min < until < expected_max


class TestConnectError:
    def test_increments_counter(self, db):
        acc = _make_account(fail_count=0)
        db.add_account(acc)

        db.on_connect_error(acc.phone, "unknown")

        restored = db.get_all_accounts()[0]
        assert restored.connect_fail_count == 1

    def test_flags_needs_reauth_at_threshold(self, db):
        acc = _make_account(fail_count=CONNECT_FAIL_THRESHOLD - 1)
        db.add_account(acc)

        db.on_connect_error(acc.phone, "auth_key_unregistered")

        restored = db.get_all_accounts()[0]
        assert restored.status == ACCOUNT_STATUS_NEEDS_REAUTH


class TestGetActiveAccounts:
    def test_returns_active(self, db):
        db.add_account(_make_account(phone="+79001111111",
                                      status=ACCOUNT_STATUS_ACTIVE))

        active = db.get_active_accounts()
        assert len(active) == 1
        assert active[0].phone == "+79001111111"

    def test_excludes_banned(self, db):
        db.add_account(_make_account(phone="+79002222222",
                                      status=ACCOUNT_STATUS_BANNED))

        active = db.get_active_accounts()
        assert active == []

    def test_excludes_needs_reauth(self, db):
        db.add_account(_make_account(phone="+79003333333",
                                      status=ACCOUNT_STATUS_NEEDS_REAUTH))

        active = db.get_active_accounts()
        assert active == []

    def test_includes_network_issue_when_cooldown_expired(self, db):
        """network_issue с истёкшим cooldown попадает в выборку для retry."""
        past = (datetime.now() - timedelta(minutes=1)).isoformat()
        db.add_account(_make_account(
            phone="+79004444444",
            status=ACCOUNT_STATUS_NETWORK_ISSUE,
            flood_until=past,
        ))

        active = db.get_active_accounts()
        assert len(active) == 1
        assert active[0].phone == "+79004444444"

    def test_excludes_network_issue_when_cooldown_active(self, db):
        future = (datetime.now() + timedelta(minutes=5)).isoformat()
        db.add_account(_make_account(
            phone="+79005555555",
            status=ACCOUNT_STATUS_NETWORK_ISSUE,
            flood_until=future,
        ))

        active = db.get_active_accounts()
        assert active == []

    def test_excludes_flooded_active(self, db):
        """active аккаунт с flood_until в будущем — не брать."""
        future = (datetime.now() + timedelta(minutes=30)).isoformat()
        db.add_account(_make_account(
            phone="+79006666666",
            status=ACCOUNT_STATUS_ACTIVE,
            flood_until=future,
        ))

        active = db.get_active_accounts()
        assert active == []

    def test_excludes_is_active_false(self, db):
        db.add_account(_make_account(phone="+79007777777",
                                      is_active=False,
                                      status=ACCOUNT_STATUS_ACTIVE))

        active = db.get_active_accounts()
        assert active == []


class TestSetAccountFloodUntil:
    def test_sets_flood_until(self, db):
        db.add_account(_make_account(phone="+79008888888"))
        future = (datetime.now() + timedelta(hours=1)).isoformat()

        db.set_account_flood_until("+79008888888", future)

        restored = db.get_all_accounts()[0]
        assert restored.flood_until == future


class TestActivateAccount:
    def test_clears_all_auto_flags(self, db):
        """Ручная реактивация сбрасывает status, fail-counter, flood_until."""
        acc = _make_account(
            phone="+79009999999",
            status=ACCOUNT_STATUS_NEEDS_REAUTH,
            fail_count=5,
            flood_until=(datetime.now() + timedelta(hours=1)).isoformat(),
            is_active=False,
        )
        db.add_account(acc)

        db.activate_account("+79009999999")

        restored = db.get_all_accounts()[0]
        assert restored.is_active is True
        assert restored.status == ACCOUNT_STATUS_ACTIVE
        assert restored.connect_fail_count == 0
        assert restored.flood_until == ""


class TestDeactivateAccount:
    def test_sets_banned_status(self, db):
        db.add_account(_make_account(phone="+79001010101"))

        db.deactivate_account("+79001010101")

        restored = db.get_all_accounts()[0]
        assert restored.is_active is False
        assert restored.status == ACCOUNT_STATUS_BANNED

        health = db.get_accounts_health()[0]
        assert health["health"] == ACCOUNT_STATUS_BANNED


class TestSetAccountStatus:
    def test_banned_status_disables_account_and_reports_banned_health(self, db):
        db.add_account(_make_account(phone="+79002020202", is_active=True))

        db.set_account_status(
            "+79002020202",
            ACCOUNT_STATUS_BANNED,
            "PhoneNumberBannedError",
        )

        restored = db.get_all_accounts()[0]
        assert restored.is_active is False
        assert restored.status == ACCOUNT_STATUS_BANNED

        health = db.get_accounts_health()[0]
        assert health["is_active"] is False
        assert health["health"] == ACCOUNT_STATUS_BANNED
