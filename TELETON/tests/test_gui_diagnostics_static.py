from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stats_frame_uses_diagnostics_snapshot():
    src = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "get_diagnostics_snapshot" in src
    assert "_get_runtime_diagnostics" in src
    assert "_schedule_diagnostics_refresh" in src


def test_broadcast_error_brief_uses_human_reason():
    src = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "def _task_error_brief" in src
    task_fn = src.split("def _task_error_brief", 1)[1].split("def _shorten_ui", 1)[0]
    assert "human_reason" in task_fn


def test_log_queue_routing_still_handles_broadcast_tags():
    src = (ROOT / "gui.py").read_text(encoding="utf-8")
    poll_fn = src.split("def _poll_queue", 1)[1].split("def _auto_resume_cycles", 1)[0]
    assert 'tag.startswith("broadcast")' in poll_fn
    assert 'tag.startswith("cycle")' in poll_fn


def test_broadcast_dashboard_refresh_button_updates_status_and_tasks_table():
    src = (ROOT / "gui.py").read_text(encoding="utf-8")
    assert "command=self._refresh_broadcast_dashboard" in src
    refresh_fn = src.split("def _refresh_broadcast_dashboard", 1)[1].split("def _refresh_broadcast_status_panel", 1)[0]
    assert "self._refresh_broadcast_status_panel()" in refresh_fn
    assert "self._tasks_embed.refresh()" in refresh_fn
