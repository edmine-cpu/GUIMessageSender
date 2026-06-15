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


def test_cycle_campaign_and_state_survive_reopen(tmp_db_path):
    db = Database(tmp_db_path)
    try:
        campaign_id = db.get_or_create_cycle_campaign("CycleBroadcast")
        db.update_cycle_campaign(
            campaign_id,
            targets_source="template",
            template_id=42,
            message_source="templates",
            message_text="hello\nworld",
            unique_mode="Спинтакс",
            enabled=True,
            account_filter="+19990001122",
            rotate_after_n_sends=7,
            send_delay_min_seconds=15,
            send_delay_max_seconds=45,
            round_pause_seconds=120,
        )
        added, updated = db.replace_cycle_targets(
            campaign_id,
            ["https://t.me/a", "https://t.me/b"],
            {
                "hours_start": 8,
                "hours_end": 22,
                "interval_min_seconds": 30,
                "interval_max_seconds": 90,
                "min_new_messages": 3,
                "fallback_hours": 6,
            },
        )
        assert added == 2
        assert updated == 0

        db.update_cycle_state(
            campaign_id,
            current_pos=1,
            last_target_link="https://t.me/b",
            last_run_at="2025-01-01T10:00:00",
            last_account_phone="+10000000001",
            last_text_preview="preview",
        )
        db.set_cycle_state_account_send_count(campaign_id, 3)
        db.add_cycle_state_stats(campaign_id, sent_inc=10, error_inc=2, last_error="oops")
        db.set_cycle_campaign_enabled(campaign_id, False)
    finally:
        db.close()

    db2 = Database(tmp_db_path)
    try:
        campaign = db2.load_cycle_campaign(campaign_id)
        state = db2.load_cycle_state(campaign_id)
        targets = db2.get_cycle_targets(campaign_id)

        assert campaign is not None
        assert campaign["targets_source"] == "template"
        assert campaign["template_id"] == 42
        assert campaign["message_source"] == "templates"
        assert campaign["message_text"] == "hello\nworld"
        assert campaign["unique_mode"] == "Спинтакс"
        assert campaign["enabled"] == 0
        assert campaign["account_filter"] == "+19990001122"
        assert campaign["rotate_after_n_sends"] == 7
        assert campaign["send_delay_min_seconds"] == 15
        assert campaign["send_delay_max_seconds"] == 45
        assert campaign["round_pause_seconds"] == 120

        assert state["current_pos"] == 1
        assert state["last_target_link"] == "https://t.me/b"
        assert state["last_run_at"] == "2025-01-01T10:00:00"
        assert state["last_account_phone"] == "+10000000001"
        assert state["last_text_preview"] == "preview"
        assert state["last_account_send_count"] == 3
        assert state["sent_total"] == 10
        assert state["error_total"] == 2
        assert state["last_error"] == "oops"

        assert [t["link"] for t in targets] == ["https://t.me/a", "https://t.me/b"]
    finally:
        db2.close()
