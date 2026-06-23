import os
import tempfile

import pytest

from ads_database import AdsDB
from ads_models import Ad
from ads_scheduler import AdsScheduler
from database import Database
from models import Account, ACCOUNT_STATUS_NEEDS_REAUTH


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


class _UnauthorizedClient:
    def __init__(self):
        self.connected = False
        self.disconnected = False

    async def connect(self):
        self.connected = True

    async def is_user_authorized(self):
        return False

    async def disconnect(self):
        self.disconnected = True
        self.connected = False

    def is_connected(self):
        return self.connected


@pytest.mark.asyncio
async def test_ads_tick_marks_unauthorized_account_needs_reauth(tmp_db_path):
    phone = "+79990001122"
    main_db = Database(tmp_db_path)
    try:
        main_db.add_account(Account(phone=phone, api_id=2040, api_hash="hash"))
    finally:
        main_db.close()

    ads_db = AdsDB(tmp_db_path)
    try:
        ads_db.add_ad(Ad(title="ad", text_base="text", active=True, account_phone=phone))
    finally:
        ads_db.close()

    client = _UnauthorizedClient()
    logs = []
    scheduler = AdsScheduler(
        tmp_db_path,
        phone,
        client_factory=lambda: client,
        log_cb=logs.append,
        tick_interval=999,
    )

    await scheduler._tick()

    check_db = Database(tmp_db_path)
    try:
        restored = check_db.get_all_accounts()[0]
    finally:
        check_db.close()

    assert client.connected is False
    assert client.disconnected is True
    assert restored.status == ACCOUNT_STATUS_NEEDS_REAUTH
    assert "is_user_authorized=False" in restored.last_status_change
    assert any("not authorized" in msg for msg in logs)
