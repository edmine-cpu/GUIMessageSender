import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_gui_source():
    return (ROOT / "gui.py").read_text(encoding="utf-8")


def _load_class_method_source(class_name: str, method_name: str):
    src = _load_gui_source()
    tree = ast.parse(src)
    classes = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert classes, f"{class_name} must exist"
    methods = [
        node
        for node in classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == method_name
    ]
    assert methods, f"{class_name}.{method_name} must exist"
    return ast.get_source_segment(src, methods[0])


def _load_top_level_function(name: str):
    src = _load_gui_source()
    tree = ast.parse(src)
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    assert nodes, f"{name} must exist"
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename="gui.py", mode="exec"), namespace)
    return namespace[name]


def test_stats_frame_uses_diagnostics_snapshot():
    src = _load_gui_source()
    assert "get_diagnostics_snapshot" in src
    assert "_get_runtime_diagnostics" in src
    assert "_schedule_diagnostics_refresh" in src


def test_broadcast_error_brief_uses_human_reason():
    src = _load_gui_source()
    assert "def _task_error_brief" in src
    task_fn = src.split("def _task_error_brief", 1)[1].split("def _shorten_ui", 1)[0]
    assert "human_reason" in task_fn


def test_log_queue_routing_still_handles_broadcast_tags():
    src = _load_gui_source()
    poll_fn = src.split("def _poll_queue", 1)[1].split("def _auto_resume_cycles", 1)[0]
    assert 'tag.startswith("broadcast")' in poll_fn
    assert 'tag.startswith("cycle")' in poll_fn


def test_broadcast_dashboard_refresh_button_updates_status_and_tasks_table():
    panel_fn = _load_class_method_source("BroadcastFrame", "_build_broadcast_status_panel")
    feedback_fn = _load_class_method_source("BroadcastFrame", "_set_broadcast_refresh_feedback")
    cycle_fn = _load_class_method_source("BroadcastFrame", "_broadcast_cycle_status_snapshot")
    status_fn = _load_class_method_source("BroadcastFrame", "_refresh_broadcast_status_panel")
    refresh_fn = _load_class_method_source("BroadcastFrame", "_refresh_broadcast_dashboard")

    assert "self.btn_broadcast_dashboard_refresh = ctk.CTkButton" in panel_fn
    assert "command=self._refresh_broadcast_dashboard" in panel_fn
    assert "self.btn_broadcast_dashboard_refresh.grid" in panel_fn
    assert "self.lbl_broadcast_refreshed = ctk.CTkLabel" in panel_fn
    assert "self.lbl_broadcast_refreshed.grid" in panel_fn

    assert "self._refresh_broadcast_status_panel()" in refresh_fn
    assert "self._tasks_embed.refresh()" in refresh_fn
    assert 'self._set_broadcast_refresh_feedback("running")' in refresh_fn
    assert 'self._set_broadcast_refresh_feedback("done"' in refresh_fn
    assert 'self._set_broadcast_refresh_feedback("error"' in refresh_fn

    assert "btn.configure" in feedback_fn
    assert "label.configure" in feedback_fn
    assert "_broadcast_dashboard_refresh_count" in feedback_fn
    assert "update_idletasks()" in feedback_fn
    assert "Обновля" in feedback_fn
    assert "Обновлено" in feedback_fn
    assert "self.after(" in feedback_fn
    assert "Обновить" in feedback_fn

    assert "self._cycle_active_names()" in cycle_fn
    assert "self._cycle_build_snapshot()" in cycle_fn
    assert "cycle_info = self._broadcast_cycle_status_snapshot()" in status_fn
    assert "Цикл выполняется" in status_fn
    assert "ошибки целей" in status_fn
    assert "Ошибка цикла" in status_fn

def test_message_template_variants_preserve_multiline_text():
    split_variants = _load_top_level_function("_split_message_template_variants")
    text = "line one\nline two\nline three"
    assert split_variants(text) == [text]


def test_message_template_variants_split_only_on_explicit_separator():
    split_variants = _load_top_level_function("_split_message_template_variants")
    assert split_variants("first\n---\nsecond\nthird") == ["first", "second\nthird"]


def test_message_template_modes_use_multiline_variant_helper():
    src = _load_gui_source()
    assert "_templates_m = _split_message_template_variants(message)" in src
    assert "templates_cache = _split_message_template_variants(msg_text)" in src
    assert "templates = _split_message_template_variants(task.message_text)" in src
