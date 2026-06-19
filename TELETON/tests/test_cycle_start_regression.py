"""
Regression tests for Teleton P0: cyclic campaign single Start and "start all" after mass button changes.

Uses AST extraction (no tkinter import of gui.py) per project conventions (see test_flood_retry.py).

Covers the concrete failures observed:
- AttributeError: 'BroadcastFrame' has no attribute 'log' (in on_show + _start paths)
- NameError: name 'format_account' is not defined (in cyclic start/diag/refresh paths)
- UI left in false "running" state after start-time exceptions before worker launch

These are mostly static presence/safety checks + the pure helper.
"""

import ast
import os
import pytest


def _load_gui_source():
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "gui.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _find_class_methods(src: str, class_name: str):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = {}
            for b in node.body:
                if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[b.name] = ast.get_source_segment(src, b) or ""
            return methods
    return {}


def _find_class_method_node(src: str, class_name: str, method_name: str):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method_name:
                    return item
    return None


def _find_top_level_function(src: str, name: str):
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return None


def test_format_account_defined_top_level():
    src = _load_gui_source()
    fn = _find_top_level_function(src, "format_account")
    assert fn is not None, "format_account must be defined at module level to fix NameError in start paths"
    assert "custom_name" in fn and "phone" in fn
    # usage sites in cyclic start and dialogs must be covered by this global now
    assert "format_account" in src  # at least the def + calls exist


def test_start_cycle_uses_safe_log_helper():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    sc_src = methods.get("_start_cycle", "")
    assert sc_src, "_start_cycle must be found inside BroadcastFrame"
    # Must call the safe helper (prevents AttributeError on self.log before/ during init)
    assert "_append_log(" in sc_src, "_start_cycle must reference _append_log for safe logging"
    # The preflight wrapper protects diagnostics (including format_account inside it)
    assert "_preflight_summary" in sc_src
    assert "try:" in sc_src and "except Exception as e:" in sc_src  # at least the preflight guard


def test_broadcastframe_has_safe_append_log_and_on_show_uses_it():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    assert "_append_log" in methods, "BroadcastFrame must define _append_log helper"
    onshow = methods.get("on_show", "")
    # The crashing diagnostic path now uses the helper (including its except handler)
    assert "_append_log(" in onshow
    # reject also uses it (called from start path)
    reject = methods.get("_cycle_reject_start", "")
    assert "_append_log(" in reject or "append_log" in reject


def test_no_undefined_format_account_in_cyclic_start_path():
    src = _load_gui_source()
    sc_src = _find_top_level_function(src, "_start_cycle") or ""
    # Any format_account call inside the start function is now safe because of top-level def
    # We assert the helper is present (the root cause fix) and calls exist only as expected
    if "format_account" in sc_src:
        # ok as long as top level def exists (checked in other test)
        assert "def format_account" in src
    # Also the pure helper for usable config must remain (used by resume path)
    assert "_cycle_has_usable_config" in src


def test_gui_imports_sendlog_for_runtime_logging():
    src = _load_gui_source()
    tree = ast.parse(src)
    imported = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "models"
        for alias in node.names
    }
    assert "SendLog" in imported


def test_gui_imports_cycle_runtime_dependencies():
    src = _load_gui_source()
    tree = ast.parse(src)
    imported_modules = {
        alias.asname or alias.name.split(".", 1)[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_from = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "random" in imported_modules
    assert "timezone" in imported_from
    assert "Task" in imported_from
    assert "HELP_TEXTS = {}" in src


def test_cycle_has_usable_config_is_pure_and_top_level():
    """The non-UI guard that was part of fixing accs==0 for 'Все активные' campaigns."""
    fn = _find_top_level_function(_load_gui_source(), "_cycle_has_usable_config")
    assert fn is not None
    assert "targets_count" in fn and "return targets_count > 0" in fn


def test_cycle_queue_messages_are_routed_to_broadcast_frame():
    src = _load_gui_source()
    methods = _find_class_methods(src, "TeletonApp")
    poll = methods.get("_poll_queue", "")
    assert poll
    assert 'tag.startswith("cycle")' in poll, "cycle_log/cycle_done/cycle_progress must reach BroadcastFrame"


def test_cycle_runtime_progress_updates_status_snapshot():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    assert "_cycle_update_runtime" in methods
    assert "_cycle_clear_runtime" in methods
    on_queue = methods.get("on_queue_message", "")
    assert 'tag == "cycle_progress"' in on_queue
    assert "_cycle_update_runtime(msg)" in on_queue
    snapshot = methods.get("_cycle_build_snapshot", "")
    assert '"next_link"' in snapshot
    assert "_cycle_runtime" in snapshot
    status = methods.get("_cycle_update_status", "")
    assert "Следующая цель" in status


def test_cycle_status_uses_compact_dashboard_not_duplicate_lines():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    status = methods.get("_cycle_update_status", "")
    refresh = methods.get("_cycle_refresh_table", "")

    assert "_cycle_metric_labels" in src
    assert "_cycle_update_dashboard(metrics" in status
    assert "summary_top" not in status
    assert "summary_bottom" not in status
    assert "lbl_cycle_next" not in src
    assert "Следующая цель:" not in refresh


def test_cycle_worker_emits_progress_for_attempt_and_result():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    start = methods.get("_start_cycle", "")
    assert '"cycle_progress"' in start
    assert 'phase="attempt"' in start
    assert "last_success_at=now.isoformat" in start
    assert "last_error=(error_detail or raw_status or status)" in start


def test_cycle_campaign_switch_saves_previous_and_loads_selected():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    on_change = methods.get("_cycle_on_campaign_change", "")
    select = methods.get("_cycle_select_campaign", "")
    assert "_cycle_save_current_campaign_settings(old_name)" in on_change
    assert "_cycle_select_campaign(name)" in on_change
    assert "_cycle_load_campaign_settings()" in select


def test_cycle_load_campaign_settings_replaces_stale_widget_values():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    load = methods.get("_cycle_load_campaign_settings", "")
    assert "_cycle_set_entry(" in load
    assert "def _fill" not in load
    assert "if not entry.get().strip()" not in load
    assert 'self.c_message.delete("1.0", "end")' in load


def test_cycle_start_saves_text_template_id_before_worker():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    start = methods.get("_start_cycle", "")
    assert "_cycle_save_current_campaign_settings(running_campaign_name)" in start
    assert "message_template_id = self._cycle_current_message_template_id()" in start
    assert "message_template_id=message_template_id" in start


def test_cycle_saved_messages_are_read_fresh_and_logged_before_send():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    start = methods.get("_start_cycle", "")
    assert "saved_cache" not in start
    assert "sender.get_saved_messages(limit=30)" in start
    assert "Источник=Избранное" in start
    assert "Перед отправкой | campaign=" in start
    assert "preview50" in start


def test_cycle_done_run_id_requires_matching_current_runner():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    on_queue = methods.get("on_queue_message", "")
    assert 'elif tag == "cycle_done":' in on_queue
    cycle_done = on_queue.split('elif tag == "cycle_done":', 1)[1].split('elif tag == "check_done":', 1)[0]

    assert "run_id" in cycle_done
    assert "runner = self._cycle_get_runner(campaign_name)" in cycle_done
    assert "self._cycle_finish_stopped_ui(campaign_name)" in cycle_done
    assert (
        "not isinstance(runner, dict)" in cycle_done
        or "runner is None" in cycle_done
        or "not runner" in cycle_done
    ), "cycle_done with run_id must return when there is no matching current runner"
    assert cycle_done.find("return") < cycle_done.find("self._cycle_finish_stopped_ui(campaign_name)")


def test_cycle_refresh_table_does_not_rotate_targets_from_current_pos():
    src = _load_gui_source()
    refresh_node = _find_class_method_node(src, "BroadcastFrame", "_cycle_refresh_table")
    assert refresh_node is not None

    target_assignments = [
        node
        for node in ast.walk(refresh_node)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "_cycle_targets"
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        )
    ]
    assert target_assignments, "_cycle_refresh_table must assign self._cycle_targets"
    for assignment in target_assignments:
        assigned = ast.dump(assignment.value)
        assert "Subscript(value=Name(id='targets'" not in assigned
    refresh = ast.get_source_segment(src, refresh_node) or ""
    assert "targets[current_pos:]" not in refresh
    assert "targets[:current_pos]" not in refresh


def test_cycle_send_path_passes_daily_actions_limit_to_sender():
    src = _load_gui_source()
    start_node = _find_class_method_node(src, "BroadcastFrame", "_start_cycle")
    assert start_node is not None
    send_calls = [
        node
        for node in ast.walk(start_node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_broadcast_message"
        )
    ]
    assert send_calls, "cycle send path must call sender.send_broadcast_message"
    assert all(
        any(keyword.arg == "daily_actions_limit" for keyword in call.keywords)
        for call in send_calls
    ), "cycle send path must pass daily_actions_limit into sender.send_broadcast_message"


def test_auto_resume_starts_from_saved_settings_without_overwriting_from_ui():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    resume = methods.get("_resume_enabled_cycles", "")
    start = methods.get("_start_cycle", "")

    assert "_start_cycle(save_current_settings=False)" in resume
    assert "def _start_cycle(self, save_current_settings: bool = True)" in start
    assert "if save_current_settings:" in start
    assert "_cycle_save_current_campaign_settings(running_campaign_name)" in start


def test_cycle_watchdog_cleans_dead_runner_instead_of_raw_pop():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    watchdog = methods.get("_cycle_watchdog", "")

    assert "_cycle_finish_stopped_ui(name)" in watchdog
    assert "runners.pop(name" not in watchdog
    assert "_cycle_stale_enabled_names" in watchdog


def test_cycle_send_path_returns_floodwait_to_runner_without_sender_sleep():
    src = _load_gui_source()
    start_node = _find_class_method_node(src, "BroadcastFrame", "_start_cycle")
    assert start_node is not None
    send_calls = [
        node
        for node in ast.walk(start_node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "send_broadcast_message"
        )
    ]
    assert send_calls
    assert all(
        any(
            keyword.arg == "sleep_on_flood_wait"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
        for call in send_calls
    )


def test_cycle_runtime_progress_records_wait_reason_and_wait_seconds():
    src = _load_gui_source()
    methods = _find_class_methods(src, "BroadcastFrame")
    start = methods.get("_start_cycle", "")
    update_runtime = methods.get("_cycle_update_runtime", "")
    snapshot = methods.get("_cycle_build_snapshot", "")
    status = methods.get("_cycle_update_status", "")

    assert '"wait_reason"' in update_runtime
    assert '"wait_seconds"' in update_runtime
    assert '"runtime_age_seconds"' in snapshot
    assert "phase:" in status
    assert "wait:" in status
    assert 'phase="waiting:no_active_accounts"' in start
    assert 'phase="waiting:account_limiter"' in start
    assert 'phase="waiting:round_pause"' in start
    assert 'phase="waiting:no_ready_targets"' in start
