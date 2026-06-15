import os
import tempfile

import pytest

from database import Database


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


def test_cycle_campaign_accounts_persist_and_keep_order(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        cid = db.get_or_create_cycle_campaign("ThemeA")
        db.set_cycle_campaign_accounts(cid, ["+100", "+200", "+300"])
        assert db.get_cycle_campaign_account_phones(cid) == ["+100", "+200", "+300"]
    finally:
        db.close()

    db2 = Database(tmp_db_path)
    try:
        cid2 = db2.get_or_create_cycle_campaign("ThemeA")
        assert db2.get_cycle_campaign_account_phones(cid2) == ["+100", "+200", "+300"]
        db2.set_cycle_campaign_accounts(cid2, ["+200", "+200", "  ", "+100"])
        assert db2.get_cycle_campaign_account_phones(cid2) == ["+200", "+100"]
    finally:
        db2.close()

