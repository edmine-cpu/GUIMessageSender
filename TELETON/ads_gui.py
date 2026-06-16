"""
ads_gui.py — GUI-компоненты планировщика объявлений.

Содержит:
  AdsMainFrame       — главный фрейм с CTkTabview (4 вкладки)
  GroupsTab          — управление группами-назначениями
  AdsTab             — управление объявлениями
  SchedulerTab       — управление планировщиком
  HistoryTab         — журнал публикаций
  GroupDialog        — диалог добавления/редактирования группы
  AdDialog           — диалог добавления/редактирования объявления
  SubsDialog         — диалог управления обязательными подписками

Все классы используют те же паттерны что и существующий gui.py:
  - CTkFrame с fg_color="transparent"
  - ScrollableTable и LogFrame из gui.py
  - AdsDB открывается локально внутри каждого метода и закрывается после
  - Тяжёлые операции (AI, Telethon) в отдельных потоках через threading.Thread
  - Результаты из потоков через after() / queue
"""

import os
import threading

import customtkinter as ctk

from ads_database import AdsDB
from ads_models import (
    Ad, GroupTarget, SchedulerSettings,
    GROUP_STATUS_ACTIVE, GROUP_STATUS_PAUSED,
)
from ads_scheduler import AdsScheduler, clamp_settings

_ADS_SCHEDULERS_GUARD = threading.Lock()
_ADS_RUNNING_SCHEDULERS: dict[str, AdsScheduler] = {}


def _ads_scheduler_alive(scheduler: AdsScheduler) -> bool:
    try:
        return bool(scheduler.is_alive)
    except Exception:
        return False


def _prune_ads_schedulers_locked():
    for phone, scheduler in list(_ADS_RUNNING_SCHEDULERS.items()):
        if not _ads_scheduler_alive(scheduler):
            _ADS_RUNNING_SCHEDULERS.pop(phone, None)


def stop_all_ads_schedulers(log_cb=None) -> int:
    """Stop every ads scheduler registered by any Ads UI tab."""
    with _ADS_SCHEDULERS_GUARD:
        _prune_ads_schedulers_locked()
        schedulers = list(_ADS_RUNNING_SCHEDULERS.items())

    stopped = 0
    for phone, scheduler in schedulers:
        try:
            did_stop = scheduler.stop()
            if did_stop:
                stopped += 1
                with _ADS_SCHEDULERS_GUARD:
                    if _ADS_RUNNING_SCHEDULERS.get(phone) is scheduler:
                        _ADS_RUNNING_SCHEDULERS.pop(phone, None)
                if log_cb:
                    log_cb(f"[ads] остановлен планировщик {phone}")
            elif log_cb:
                log_cb(f"[ads] планировщик {phone} ещё останавливается")
        except Exception as e:
            if log_cb:
                log_cb(f"[ads] ошибка остановки {phone}: {e}")
    return stopped


# Импортируем виджеты из gui.py — они уже загружены в рантайме
def _get_gui_widgets():
    """Получить ScrollableTable и LogFrame из gui модуля."""
    import gui as _gui
    return _gui.ScrollableTable, _gui.LogFrame


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def _run_loop(loop, coro):
    """Запустить корутину в event loop.

    Делегируем в gui._run_loop — единая реализация: cancel pending tasks,
    gather, close. Раньше здесь был дубликат, который не закрывал loop
    (утечка) и не отменял pending tasks.
    """
    import gui as _gui
    return _gui._run_loop(loop, coro)


# ─── Диалог: группа ──────────────────────────────────────────────────────────

class GroupDialog(ctk.CTkToplevel):
    """Диалог добавления / редактирования группы-назначения."""

    def __init__(self, master, app, group: GroupTarget = None):
        super().__init__(master)
        self.app = app
        self.group = group  # None = добавление, GroupTarget = редактирование
        self.result = None  # (GroupTarget, [str]) после OK, None после Cancel

        # Список подписок в памяти — список строк (ссылок)
        self._subs: list = []

        self.title("Редактировать группу" if group else "Добавить группу")
        self.geometry("500x720")
        self.resizable(False, True)
        self.grab_set()

        self._build()
        if group:
            self._populate(group)

    def _build(self):
        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=15, pady=10)

        def row(label, widget_factory, r):
            ctk.CTkLabel(frame, text=label, anchor="w").grid(
                row=r, column=0, padx=5, pady=4, sticky="w")
            w = widget_factory(frame)
            w.grid(row=r, column=1, padx=5, pady=4, sticky="ew")
            frame.grid_columnconfigure(1, weight=1)
            return w

        # Поле ссылки — multi-line, чтобы юзер мог вставить несколько ссылок
        # сразу. Парсер _parse_links_input разбирает по переносам/запятым.
        ctk.CTkLabel(frame, text="Ссылка / @username:", anchor="nw").grid(
            row=0, column=0, padx=5, pady=4, sticky="nw")
        link_box = ctk.CTkFrame(frame, fg_color="transparent")
        link_box.grid(row=0, column=1, padx=5, pady=4, sticky="ew")
        link_box.grid_columnconfigure(0, weight=1)
        self.e_link = ctk.CTkTextbox(link_box, height=60)
        self.e_link.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            link_box,
            text="одна ссылка или несколько через перенос строки",
            text_color="gray60",
            font=ctk.CTkFont(size=11),
            anchor="w",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        frame.grid_columnconfigure(1, weight=1)

        self.e_title = row("Название:", lambda p: ctk.CTkEntry(
            p, placeholder_text="Доска объявлений Москва"), 1)
        self.e_category = row("Категория / теги:", lambda p: ctk.CTkEntry(
            p, placeholder_text="продажа, авто, электроника"), 2)
        self.e_interval = row("Мин. интервал (мин):", lambda p: ctk.CTkEntry(
            p, placeholder_text="60"), 3)
        # Макс. интервал — для рандома между min и max. 0 = без рандома (фикс. интервал = min).
        self.e_interval_max = row(
            "Макс. интервал (мин, 0 = без рандома):",
            lambda p: ctk.CTkEntry(p, placeholder_text="0"), 4)
        self.e_hours_start = row("Час начала (0-23):", lambda p: ctk.CTkEntry(
            p, placeholder_text="0"), 5)
        self.e_hours_end = row("Час конца (0-23):", lambda p: ctk.CTkEntry(
            p, placeholder_text="23"), 6)

        ctk.CTkLabel(frame, text="Заметки (правила):", anchor="w").grid(
            row=7, column=0, padx=5, pady=4, sticky="nw")
        self.e_notes = ctk.CTkTextbox(frame, height=70)
        self.e_notes.grid(row=7, column=1, padx=5, pady=4, sticky="ew")

        # Статус
        ctk.CTkLabel(frame, text="Статус:", anchor="w").grid(
            row=8, column=0, padx=5, pady=4, sticky="w")
        self.status_var = ctk.StringVar(value=GROUP_STATUS_ACTIVE)
        ctk.CTkSegmentedButton(
            frame, values=["активна", "пауза"],
            variable=self.status_var,
        ).grid(row=8, column=1, padx=5, pady=4, sticky="w")

        # ── Обязательные подписки ──────────────────────────────────────────
        ctk.CTkLabel(
            frame,
            text="Обязательные подписки:",
            anchor="w",
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=9, column=0, columnspan=2, padx=5, pady=(12, 2), sticky="w")

        ctk.CTkLabel(
            frame,
            text="Каналы/группы, в которых нужно состоять перед публикацией",
            anchor="w",
            text_color="gray60",
            font=ctk.CTkFont(size=11),
        ).grid(row=10, column=0, columnspan=2, padx=5, pady=(0, 4), sticky="w")

        # Список подписок
        self.subs_list_frame = ctk.CTkFrame(frame, fg_color=("gray85", "gray20"),
                                             corner_radius=6)
        self.subs_list_frame.grid(row=11, column=0, columnspan=2,
                                   padx=5, pady=4, sticky="ew")

        # Строка добавления
        add_row = ctk.CTkFrame(frame, fg_color="transparent")
        add_row.grid(row=12, column=0, columnspan=2, padx=5, pady=4, sticky="ew")
        add_row.grid_columnconfigure(0, weight=1)

        self.e_new_sub = ctk.CTkEntry(
            add_row, placeholder_text="@channel или t.me/...")
        self.e_new_sub.grid(row=0, column=0, padx=(0, 5), sticky="ew")
        ctk.CTkButton(
            add_row, text="+ Добавить", width=100,
            command=self._add_sub,
        ).grid(row=0, column=1)

        # Нарисовать начальный список (пустой при создании)
        self._render_subs()

        # Кнопки OK / Отмена
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=15, pady=(5, 10))
        ctk.CTkButton(btn_row, text="OK", width=100,
                       command=self._ok).pack(side="right", padx=5)
        ctk.CTkButton(btn_row, text="Отмена", width=100,
                       fg_color="gray40", hover_color="gray30",
                       command=self.destroy).pack(side="right", padx=5)

    def _render_subs(self):
        """Перерисовать список подписок в subs_list_frame."""
        for w in self.subs_list_frame.winfo_children():
            w.destroy()

        if not self._subs:
            ctk.CTkLabel(
                self.subs_list_frame,
                text="Нет обязательных подписок",
                text_color="gray60",
            ).pack(padx=8, pady=6)
            return

        for link in self._subs:
            row = ctk.CTkFrame(self.subs_list_frame, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            ctk.CTkLabel(row, text=f"📌 {link}", anchor="w").pack(
                side="left", padx=5, expand=True, fill="x")
            ctk.CTkButton(
                row, text="✕", width=28,
                fg_color="firebrick", hover_color="darkred",
                command=lambda lnk=link: self._remove_sub(lnk),
            ).pack(side="right", padx=3)

    def _add_sub(self):
        link = self.e_new_sub.get().strip()
        if not link:
            return
        if link not in self._subs:
            self._subs.append(link)
            self._render_subs()
        self.e_new_sub.delete(0, "end")

    def _remove_sub(self, link: str):
        if link in self._subs:
            self._subs.remove(link)
            self._render_subs()

    def _populate(self, g: GroupTarget):
        # CTkTextbox: insert("0.0", text)
        self.e_link.insert("0.0", g.link)
        self.e_title.insert(0, g.title or "")
        self.e_category.insert(0, g.category or "")
        self.e_interval.insert(0, str(g.interval_minutes))
        self.e_interval_max.insert(0, str(g.interval_minutes_max or 0))
        self.e_hours_start.insert(0, str(g.hours_start))
        self.e_hours_end.insert(0, str(g.hours_end))
        if g.notes:
            self.e_notes.insert("1.0", g.notes)
        self.status_var.set(g.status)
        # Загружаем существующие подписки из БД
        if g.id:
            db = AdsDB(self.app.config.db_path)
            subs = db.get_required_subs_for_group(g.id)
            db.close()
            self._subs = [s.channel_link for s in subs]
            self._render_subs()

    def _ok(self):
        raw = self.e_link.get("1.0", "end").strip()
        if not raw:
            ctk.CTkLabel(self, text="Ссылка обязательна",
                          text_color="red").pack()
            return

        # Парсим — может быть одна ссылка или несколько через перенос/запятую/пробел
        links = _parse_links_input(raw)
        if not links:
            ctk.CTkLabel(
                self,
                text="Не нашёл ни одной валидной ссылки. Формат: "
                     "https://t.me/groupname или @groupname",
                text_color="red",
            ).pack()
            return

        # При редактировании — только одна ссылка
        if self.group and len(links) > 1:
            ctk.CTkLabel(
                self,
                text="При редактировании — только одна ссылка",
                text_color="red",
            ).pack()
            return

        try:
            interval = int(self.e_interval.get().strip() or "60")
            interval_max = int(self.e_interval_max.get().strip() or "0")
            hours_start = int(self.e_hours_start.get().strip() or "0")
            hours_end = int(self.e_hours_end.get().strip() or "23")
        except ValueError:
            ctk.CTkLabel(self, text="Интервал и часы должны быть числами",
                          text_color="red").pack()
            return

        # Валидация: если max задан (>0), он не может быть меньше min
        if interval_max > 0 and interval_max < interval:
            ctk.CTkLabel(
                self,
                text=f"Макс. интервал ({interval_max}) меньше мин. ({interval})",
                text_color="red",
            ).pack()
            return

        # Создаём GroupTarget для каждой ссылки. Все настройки одинаковые.
        # При редактировании — links содержит ровно 1 элемент.
        groups = []
        for link in links:
            g = GroupTarget(
                id=self.group.id if (self.group and len(links) == 1) else None,
                link=link,
                title=self.e_title.get().strip(),
                category=self.e_category.get().strip(),
                interval_minutes=max(1, interval),
                interval_minutes_max=max(0, interval_max),
                hours_start=max(0, min(23, hours_start)),
                hours_end=max(0, min(23, hours_end)),
                notes=self.e_notes.get("1.0", "end").strip(),
                status=self.status_var.get(),
                join_status=self.group.join_status if self.group else "unknown",
                retry_after=self.group.retry_after if self.group else "",
                last_error=self.group.last_error if self.group else "",
            )
            groups.append(g)

        # result = (список групп, список ссылок подписок)
        # Backward-compat: если групп ровно 1 — все вызывающие места
        # могут продолжать читать groups[0]; новые места знают что это список.
        self.result = (groups, list(self._subs))
        self.destroy()


def _parse_links_input(raw: str) -> list:
    """Парсит строку с одной или несколькими ссылками.

    Поддерживает разделители: \\n, \\r, запятая, точка с запятой, пробел/таб.
    Каждая ссылка валидируется по формату:
      https://t.me/...   |   t.me/...   |   @username
    Невалидные строки выкидываются. Дубликаты (с одинаковым нормализованным
    видом) оставляются как есть — чтобы не «съесть» ссылку случайно.

    Возвращает список валидных ссылок в исходном виде.
    """
    import re
    # Разбиваем по типичным разделителям
    parts = re.split(r"[\r\n,;]+|\s{2,}", raw)
    # Дополнительно дробим по одиночным пробелам, но только если в части
    # обнаружено несколько потенциальных ссылок
    expanded = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Если в части несколько "https://" или "t.me/" — это слитые ссылки
        if p.count("http") > 1 or p.count("t.me/") > 1:
            for token in re.split(r"\s+", p):
                token = token.strip()
                if token:
                    expanded.append(token)
        else:
            expanded.append(p)

    valid = []
    pattern = re.compile(
        r"^(?:https?://)?t\.me/[A-Za-z0-9_+/]+(?:/\d+)?/?$|^@[A-Za-z][A-Za-z0-9_]{3,}$",
        re.IGNORECASE,
    )
    for link in expanded:
        link = link.strip().rstrip("/")
        if not link:
            continue
        if pattern.match(link):
            valid.append(link)
    return valid


# ─── Диалог: обязательные подписки ──────────────────────────────────────────

class SubsDialog(ctk.CTkToplevel):
    """Диалог управления обязательными подписками для группы."""

    def __init__(self, master, app, group: GroupTarget):
        super().__init__(master)
        self.app = app
        self.group = group
        self.title(f"Подписки для {group.link}")
        self.geometry("400x400")
        self.resizable(False, False)
        self.grab_set()
        self._build()
        self._refresh()

    def _build(self):
        ctk.CTkLabel(self, text="Обязательные подписки",
                      font=ctk.CTkFont(size=15, weight="bold")).pack(
            padx=15, pady=(12, 5), anchor="w")
        ctk.CTkLabel(self, text="Каналы/группы в которых нужно состоять\nперед публикацией:",
                      text_color="gray60").pack(padx=15, anchor="w")

        # Список
        self.list_frame = ctk.CTkScrollableFrame(self, height=200)
        self.list_frame.pack(fill="x", padx=15, pady=8)

        # Добавление
        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.pack(fill="x", padx=15, pady=5)
        self.e_new = ctk.CTkEntry(add_row, placeholder_text="@channel или t.me/...",
                                   width=260)
        self.e_new.pack(side="left", padx=(0, 5))
        ctk.CTkButton(add_row, text="Добавить", width=90,
                       command=self._add).pack(side="left")

        ctk.CTkButton(self, text="Закрыть", command=self.destroy).pack(
            padx=15, pady=(5, 12), anchor="e")

    def _refresh(self):
        for w in self.list_frame.winfo_children():
            w.destroy()
        db = AdsDB(self.app.config.db_path)
        subs = db.get_required_subs_for_group(self.group.id)
        db.close()

        if not subs:
            ctk.CTkLabel(self.list_frame, text="Нет требований",
                          text_color="gray60").pack(padx=5, pady=5)
            return

        for sub in subs:
            row = ctk.CTkFrame(self.list_frame, fg_color="transparent")
            row.pack(fill="x", pady=2)
            status = "✅" if sub.is_joined else "⬜"
            ctk.CTkLabel(row, text=f"{status} {sub.channel_link}",
                          anchor="w").pack(side="left", padx=5, expand=True, fill="x")
            ctk.CTkButton(
                row, text="✕", width=30, fg_color="firebrick",
                hover_color="darkred",
                command=lambda link=sub.channel_link: self._delete(link),
            ).pack(side="right", padx=3)

    def _add(self):
        link = self.e_new.get().strip()
        if not link:
            return
        db = AdsDB(self.app.config.db_path)
        db.add_required_sub(self.group.id, link)
        db.close()
        self.e_new.delete(0, "end")
        self._refresh()

    def _delete(self, link: str):
        db = AdsDB(self.app.config.db_path)
        db.delete_required_sub(self.group.id, link)
        db.close()
        self._refresh()


# ─── Диалог: объявление ──────────────────────────────────────────────────────

class AdDialog(ctk.CTkToplevel):
    """Диалог добавления / редактирования объявления."""

    def __init__(self, master, app, ad: Ad = None):
        super().__init__(master)
        self.app = app
        self.ad = ad
        self.result = None
        self.selected_groups = set()

        self.title("Редактировать объявление" if ad else "Добавить объявление")
        self.geometry("560x680")
        self.resizable(True, True)
        self.grab_set()
        self._build()
        if ad:
            self._populate(ad)

    def _build(self):
        # Основная форма
        form = ctk.CTkScrollableFrame(self)
        form.pack(fill="both", expand=True, padx=15, pady=10)
        form.grid_columnconfigure(1, weight=1)

        def lbl(text, row):
            ctk.CTkLabel(form, text=text, anchor="w").grid(
                row=row, column=0, padx=5, pady=4, sticky="nw")

        lbl("Название (внутр.):", 0)
        self.e_title = ctk.CTkEntry(form, placeholder_text="Продаю айфон #1")
        self.e_title.grid(row=0, column=1, padx=5, pady=4, sticky="ew")

        lbl("Аккаунт:", 1)
        db = AdsDB(self.app.config.db_path)
        from database import Database
        main_db = Database(self.app.config.db_path)
        accounts = [a.phone for a in main_db.get_all_accounts() if a.is_active]
        main_db.close()
        db.close()
        self.acc_var = ctk.StringVar(
            value=accounts[0] if accounts else "")
        ctk.CTkOptionMenu(form, variable=self.acc_var,
                           values=accounts or ["—"]).grid(
            row=1, column=1, padx=5, pady=4, sticky="ew")

        lbl("Текст объявления:", 2)
        self.e_text = ctk.CTkTextbox(form, height=150)
        self.e_text.grid(row=2, column=1, padx=5, pady=4, sticky="ew")

        # Кнопка AI
        ai_row = ctk.CTkFrame(form, fg_color="transparent")
        ai_row.grid(row=3, column=1, padx=5, pady=2, sticky="w")
        ctk.CTkButton(ai_row, text="✨ Сгенерировать через AI",
                       width=200, command=self._ai_generate).pack(side="left")
        self.lbl_ai_status = ctk.CTkLabel(ai_row, text="", text_color="gray60")
        self.lbl_ai_status.pack(side="left", padx=8)

        lbl("Медиа (путь к файлу):", 4)
        media_row = ctk.CTkFrame(form, fg_color="transparent")
        media_row.grid(row=4, column=1, padx=5, pady=4, sticky="ew")
        self.e_media = ctk.CTkEntry(media_row, placeholder_text="/path/to/photo.jpg")
        self.e_media.pack(side="left", expand=True, fill="x", padx=(0, 5))
        ctk.CTkButton(media_row, text="📁", width=35,
                       command=self._browse_media).pack(side="left")

        lbl("Категория / теги:", 5)
        self.e_category = ctk.CTkEntry(form, placeholder_text="авто, продажа")
        self.e_category.grid(row=5, column=1, padx=5, pady=4, sticky="ew")

        lbl("Кнопка:", 6)
        button_frame = ctk.CTkFrame(form, fg_color="transparent")
        button_frame.grid(row=6, column=1, padx=5, pady=4, sticky="ew")
        button_frame.grid_columnconfigure(0, weight=1)
        button_frame.grid_columnconfigure(1, weight=1)
        self.e_button_text = ctk.CTkEntry(
            button_frame, placeholder_text="Написать")
        self.e_button_text.grid(row=0, column=0, padx=(0, 5), sticky="ew")
        self.e_button_url = ctk.CTkEntry(
            button_frame, placeholder_text="@username, t.me/chat или https://...")
        self.e_button_url.grid(row=0, column=1, padx=(5, 0), sticky="ew")
        ctk.CTkLabel(
            button_frame,
            text="Необязательно: текст кнопки + ссылка, куда она ведёт",
            text_color="gray60",
            font=ctk.CTkFont(size=11),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # Активно
        self.active_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(form, text="Активно (публиковать)",
                         variable=self.active_var).grid(
            row=7, column=1, padx=5, pady=4, sticky="w")

        # Выбор групп
        lbl("Группы для публикации:", 8)
        self.groups_frame = ctk.CTkScrollableFrame(form, height=120)
        self.groups_frame.grid(row=8, column=1, padx=5, pady=4, sticky="ew")
        self._load_groups_list()

        # Кнопки
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=15, pady=(5, 10))
        ctk.CTkButton(btn_row, text="OK", width=100,
                       command=self._ok).pack(side="right", padx=5)
        ctk.CTkButton(btn_row, text="Отмена", width=100,
                       fg_color="gray40", hover_color="gray30",
                       command=self.destroy).pack(side="right", padx=5)

    def _load_groups_list(self):
        for w in self.groups_frame.winfo_children():
            w.destroy()
        self._group_vars = {}
        db = AdsDB(self.app.config.db_path)
        groups = db.get_all_groups()
        db.close()
        if not groups:
            ctk.CTkLabel(self.groups_frame, text="Нет групп — сначала добавьте группы",
                          text_color="gray60").pack(padx=5)
            return
        for g in groups:
            var = ctk.BooleanVar(value=g.id in self.selected_groups)
            self._group_vars[g.id] = var
            ctk.CTkCheckBox(self.groups_frame, text=f"{g.link} ({g.title or '—'})",
                             variable=var).pack(anchor="w", padx=5, pady=1)

    def _populate(self, ad: Ad):
        self.e_title.insert(0, ad.title)
        self.e_text.insert("1.0", ad.text_base)
        if ad.media_path:
            self.e_media.insert(0, ad.media_path)
        if ad.category:
            self.e_category.insert(0, ad.category)
        if ad.button_text:
            self.e_button_text.insert(0, ad.button_text)
        if ad.button_url:
            self.e_button_url.insert(0, ad.button_url)
        self.active_var.set(ad.active)
        self.acc_var.set(ad.account_phone)
        # Загружаем выбранные группы
        db = AdsDB(self.app.config.db_path)
        groups = db.get_groups_for_ad(ad.id)
        db.close()
        self.selected_groups = {g.id for g in groups}
        self._load_groups_list()

    def _browse_media(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Выбрать медиафайл",
            filetypes=[("Изображения", "*.jpg *.jpeg *.png *.gif *.webp"),
                        ("Все файлы", "*.*")])
        if path:
            self.e_media.delete(0, "end")
            self.e_media.insert(0, path)

    def _ai_generate(self):
        """Показать диалог AI-генерации текста."""
        AIGenerateDialog(self, self.app, callback=self._on_ai_result)

    def _on_ai_result(self, text: str):
        self.e_text.delete("1.0", "end")
        self.e_text.insert("1.0", text)
        self.lbl_ai_status.configure(text="✅ Текст сгенерирован")

    def _ok(self):
        text = self.e_text.get("1.0", "end").strip()
        title = self.e_title.get().strip()
        if not title:
            title = text[:40] + "..." if len(text) > 40 else text
        if not text:
            ctk.CTkLabel(self, text="Текст объявления обязателен",
                          text_color="red").pack()
            return
        button_text = self.e_button_text.get().strip()
        button_url = self.e_button_url.get().strip()
        if bool(button_text) != bool(button_url):
            ctk.CTkLabel(
                self,
                text="Для кнопки нужны и название, и ссылка",
                text_color="red").pack()
            return
        if button_url:
            try:
                from ads_publisher import normalize_button_url
                button_url = normalize_button_url(button_url)
            except ValueError as e:
                ctk.CTkLabel(self, text=f"Некорректная ссылка кнопки: {e}",
                              text_color="red").pack()
                return
        ad = Ad(
            id=self.ad.id if self.ad else None,
            title=title,
            text_base=text,
            media_path=self.e_media.get().strip(),
            category=self.e_category.get().strip(),
            button_text=button_text,
            button_url=button_url,
            active=self.active_var.get(),
            account_phone=self.acc_var.get(),
        )
        # Список выбранных групп
        selected = [gid for gid, var in self._group_vars.items() if var.get()]
        self.result = (ad, selected)
        self.destroy()


# ─── Диалог: AI генерация ────────────────────────────────────────────────────

class AIGenerateDialog(ctk.CTkToplevel):
    """Диалог для AI-генерации текста объявления."""

    def __init__(self, master, app, callback):
        super().__init__(master)
        self.app = app
        self.callback = callback
        self.title("Генерация объявления через AI")
        self.geometry("480x400")
        self.resizable(False, False)
        self.grab_set()
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text="Опишите что продаёте / предлагаете:",
                      font=ctk.CTkFont(size=14)).pack(padx=15, pady=(12, 5), anchor="w")
        self.e_desc = ctk.CTkTextbox(self, height=120)
        self.e_desc.pack(fill="x", padx=15, pady=5)
        ctk.CTkLabel(
            self,
            text="Пример: продаю iPhone 15 Pro 256GB, цвет чёрный, состояние 9/10,\n"
                 "90000 руб, Москва, торг уместен, самовывоз или доставка СДЭК",
            text_color="gray60",
        ).pack(padx=15, anchor="w")

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.pack(fill="x", padx=15, pady=8)
        ctk.CTkLabel(opts, text="Тон:").pack(side="left", padx=(0, 5))
        self.tone_var = ctk.StringVar(value="")
        ctk.CTkOptionMenu(opts, variable=self.tone_var,
                           values=["", "дружелюбный", "деловой", "срочный"],
                           width=140).pack(side="left", padx=5)
        ctk.CTkLabel(opts, text="Длина:").pack(side="left", padx=(10, 5))
        self.length_var = ctk.StringVar(value="")
        ctk.CTkOptionMenu(opts, variable=self.length_var,
                           values=["", "короткое", "подробное"],
                           width=130).pack(side="left", padx=5)

        self.btn_gen = ctk.CTkButton(self, text="✨ Сгенерировать",
                                      command=self._generate)
        self.btn_gen.pack(padx=15, pady=8)
        self.lbl_status = ctk.CTkLabel(self, text="", text_color="gray60")
        self.lbl_status.pack(padx=15)

    def _generate(self):
        desc = self.e_desc.get("1.0", "end").strip()
        if not desc:
            self.lbl_status.configure(text="Введите описание", text_color="red")
            return

        db = AdsDB(self.app.config.db_path)
        settings = db.load_scheduler_settings()
        db.close()

        openai_key = getattr(self.app.config, "openai_api_key", "")
        groq_key = getattr(self.app.config, "groq_api_key", "")

        if settings.ai_provider == "groq" and groq_key:
            provider_name, api_key, model = "groq", groq_key, settings.ai_model_groq
        elif openai_key:
            provider_name, api_key, model = "openai", openai_key, settings.ai_model_openai
        else:
            self.lbl_status.configure(
                text="Нет API-ключа. Укажите в Настройках → Планировщик объявлений",
                text_color="red")
            return

        self.btn_gen.configure(state="disabled", text="Генерирую...")
        self.lbl_status.configure(text="", text_color="gray60")
        tone = self.tone_var.get()
        length = self.length_var.get()

        # Прокси для AI-запросов — защита от утечки реального IP и содержимого
        # промптов через OpenAI/Groq API. Для Groq fallback на openai_proxy
        # если groq_proxy не задан отдельно.
        if provider_name == "groq":
            proxy = (getattr(self.app.config, "groq_proxy", "")
                     or getattr(self.app.config, "openai_proxy", ""))
        else:
            proxy = getattr(self.app.config, "openai_proxy", "")

        # L3: warning в статусе диалога если прокси не задан
        if not proxy:
            self.lbl_status.configure(
                text="⚠ AI идёт БЕЗ прокси — палится реальный IP",
                text_color="#E74C3C")
            print(f"[!!] ВНИМАНИЕ: AI-генерация объявления ({provider_name}) БЕЗ прокси")
            print("[!!] Палится реальный IP + содержимое промпта")
            print(f"[!!] Задайте {'GROQ_PROXY' if provider_name == 'groq' else 'OPENAI_PROXY'} в Настройках")

        def _thread():
            try:
                from ads_ai import make_provider, AdsAI
                provider = make_provider(provider_name, api_key, model, proxy=proxy)
                ai = AdsAI(provider)
                text = ai.generate_ad(desc, tone=tone, length=length)
                self.after(0, lambda: self._done(text))
            except Exception as exc:
                err_msg = str(exc)
                self.after(0, lambda m=err_msg: self._error(m))

        threading.Thread(target=_thread, daemon=True).start()

    def _done(self, text: str):
        self.callback(text)
        self.destroy()

    def _error(self, msg: str):
        self.btn_gen.configure(state="normal", text="✨ Сгенерировать")
        self.lbl_status.configure(text=f"Ошибка: {msg[:80]}", text_color="red")


# ─── Вкладка: Группы ─────────────────────────────────────────────────────────

class GroupsTab(ctk.CTkFrame):
    """Вкладка управления группами-назначениями."""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._groups = []
        ScrollableTable, LogFrame = _get_gui_widgets()

        # Toolbar
        tb = ctk.CTkFrame(self, fg_color="transparent")
        tb.pack(padx=10, pady=8, fill="x")

        ctk.CTkButton(tb, text="Добавить", width=110,
                       command=self._add).pack(side="left", padx=(0, 5))
        ctk.CTkButton(tb, text="Редактировать", width=130,
                       command=self._edit).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="Подписки", width=110,
                       command=self._subs).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="Пауза/Активно", width=130,
                       command=self._toggle).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="Удалить", width=90,
                       fg_color="firebrick", hover_color="darkred",
                       command=self._delete).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="Импорт из шаблона", width=150,
                       command=self._import_from_template).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="↻ Обновить", width=100,
                       command=self.refresh).pack(side="right", padx=5)

        # Таблица
        self.table = ScrollableTable(self, columns=[
            "Ссылка", "Название", "Интервал (мин)", "Часы",
            "Статус", "Подписан", "Retry до"])
        self.table.pack(padx=10, pady=5, fill="both", expand=True)

        # Лог
        self.log = LogFrame(self, height=80)
        self.log.pack(padx=10, pady=(0, 8), fill="x")

        self.refresh()

    def refresh(self):
        db = AdsDB(self.app.config.db_path)
        self._groups = db.get_all_groups()
        db.close()
        rows = []
        for g in self._groups:
            retry = g.retry_after[11:16] if g.retry_after else "—"
            hours = f"{g.hours_start:02d}-{g.hours_end:02d}"
            # Формат интервала: "60" если max=0 (без рандома), "60-120" если задан max
            if g.interval_minutes_max and g.interval_minutes_max > 0:
                interval_str = f"{g.interval_minutes}-{g.interval_minutes_max}"
            else:
                interval_str = str(g.interval_minutes)
            rows.append((
                g.link,
                g.title or "—",
                interval_str,
                hours,
                g.status,
                g.join_status,
                retry,
            ))
        self.table.set_data(rows)

    def _selected_group(self):
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите группу в таблице")
            return None
        link = row[0].lstrip("▶ ")
        for g in self._groups:
            if g.link == link:
                return g
        return None

    def _add(self):
        dlg = GroupDialog(self, self.app)
        self.wait_window(dlg)
        if dlg.result:
            groups, subs = dlg.result  # groups — список GroupTarget
            db = AdsDB(self.app.config.db_path)
            added = 0
            try:
                for group in groups:
                    try:
                        group_id = db.add_group(group)
                        for link in subs:
                            db.add_required_sub(group_id, link)
                        added += 1
                    except Exception as e:
                        self.log.append(f"[-] Не добавлено {group.link}: {e}")
                if added == 1 and len(groups) == 1:
                    self.log.append(
                        f"[+] Добавлена группа: {groups[0].link}"
                        + (f" ({len(subs)} подписок)" if subs else ""))
                else:
                    self.log.append(
                        f"[+] Добавлено групп: {added} из {len(groups)}"
                        + (f", по {len(subs)} подписок к каждой" if subs else ""))
            except Exception as e:
                self.log.append(f"[-] Ошибка: {e}")
            finally:
                db.close()

            if len(groups) > 1:
                name = ctk.CTkInputDialog(
                    text="Сохранить этот список как шаблон? Введите название или оставьте пустым:",
                    title="Сохранение шаблона").get_input()
                name = (name or "").strip()
                if name:
                    try:
                        from database import Database
                        db2 = Database(self.app.config.db_path)
                        db2.add_list_template(name, "groups", "\n".join([g.link for g in groups]))
                        db2.close()
                        self.log.append(f"[+] Шаблон сохранён: {name}")
                    except Exception as e:
                        self.log.append(f"[!] Не удалось сохранить шаблон: {e}")
            self.refresh()

    def _edit(self):
        g = self._selected_group()
        if not g:
            return
        dlg = GroupDialog(self, self.app, group=g)
        self.wait_window(dlg)
        if dlg.result:
            groups, subs = dlg.result
            # При редактировании дайл возвращает ровно один элемент
            group = groups[0]
            db = AdsDB(self.app.config.db_path)
            db.update_group(group)
            # Синхронизируем подписки: удаляем старые, добавляем новые
            old_subs = {s.channel_link
                        for s in db.get_required_subs_for_group(group.id)}
            new_subs = set(subs)
            for link in old_subs - new_subs:
                db.delete_required_sub(group.id, link)
            for link in new_subs - old_subs:
                db.add_required_sub(group.id, link)
            db.close()
            self.log.append(
                f"[~] Обновлено: {group.link}"
                + (f" ({len(subs)} подписок)" if subs else ""))
            self.refresh()

    def _subs(self):
        g = self._selected_group()
        if not g:
            return
        dlg = SubsDialog(self, self.app, g)
        self.wait_window(dlg)

    def _toggle(self):
        g = self._selected_group()
        if not g:
            return
        new_status = (GROUP_STATUS_PAUSED
                      if g.status == GROUP_STATUS_ACTIVE
                      else GROUP_STATUS_ACTIVE)
        db = AdsDB(self.app.config.db_path)
        db.set_group_status(g.id, new_status)
        db.close()
        self.log.append(f"[~] {g.link} → {new_status}")
        self.refresh()

    def _delete(self):
        g = self._selected_group()
        if not g:
            return
        db = AdsDB(self.app.config.db_path)
        db.delete_group(g.id)
        db.close()
        self.log.append(f"[-] Удалена группа: {g.link}")
        self.refresh()

    def _import_from_template(self):
        from database import Database
        from gui import ListTemplatePickerDialog
        from ads_models import GroupTarget

        db = Database(self.app.config.db_path)
        templates = [t for t in db.get_all_list_templates() if t.get("kind") in ("groups", "mixed")]
        db.close()
        if not templates:
            self.log.append("[!] Нет шаблонов групп (создай в разделе 'Шаблоны')")
            return

        pick = ListTemplatePickerDialog(self, templates, title="Импорт групп в объявления из шаблона")
        self.wait_window(pick)
        if not pick.result:
            return

        links = [l.strip() for l in (pick.result.get("content") or "").splitlines() if l.strip()]
        if not links:
            self.log.append("[!] Шаблон пустой")
            return

        added = 0
        skipped = 0
        adb = AdsDB(self.app.config.db_path)
        try:
            for link in links:
                if adb.get_group_by_link(link):
                    skipped += 1
                    continue
                adb.add_group(GroupTarget(link=link, notes=f"template:{pick.result['name']}"))
                added += 1
        except Exception as e:
            self.log.append(f"[-] Ошибка импорта: {e}")
        finally:
            adb.close()

        self.log.append(f"[+] Импортировано: {added}, пропущено (уже есть): {skipped}")
        self.refresh()


# ─── Вкладка: Объявления ─────────────────────────────────────────────────────

class AdsTab(ctk.CTkFrame):
    """Вкладка управления объявлениями."""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._ads = []
        ScrollableTable, LogFrame = _get_gui_widgets()

        # Toolbar
        tb = ctk.CTkFrame(self, fg_color="transparent")
        tb.pack(padx=10, pady=8, fill="x")

        ctk.CTkButton(tb, text="Добавить", width=110,
                       command=self._add).pack(side="left", padx=(0, 5))
        ctk.CTkButton(tb, text="Редактировать", width=130,
                       command=self._edit).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="Вкл/Выкл", width=100,
                       command=self._toggle).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="Удалить", width=90,
                       fg_color="firebrick", hover_color="darkred",
                       command=self._delete).pack(side="left", padx=5)
        ctk.CTkButton(tb, text="↻ Обновить", width=100,
                       command=self.refresh).pack(side="right", padx=5)

        # Таблица
        self.table = ScrollableTable(self, columns=[
            "Название", "Аккаунт", "Категория", "Активно", "Групп", "Медиа", "Кнопка"])
        self.table.pack(padx=10, pady=5, fill="both", expand=True)

        # Лог
        self.log = LogFrame(self, height=80)
        self.log.pack(padx=10, pady=(0, 8), fill="x")

        self.refresh()

    def refresh(self):
        db = AdsDB(self.app.config.db_path)
        self._ads = db.get_all_ads()
        rows = []
        for ad in self._ads:
            groups = db.get_groups_for_ad(ad.id)
            rows.append((
                ad.title or ad.text_base[:30],
                ad.account_phone,
                ad.category or "—",
                "Да" if ad.active else "Нет",
                len(groups),
                "✓" if ad.media_path and os.path.exists(ad.media_path) else "—",
                "Да" if (ad.button_text and ad.button_url) else "—",
            ))
        db.close()
        self.table.set_data(rows)

    def _selected_ad(self):
        row = self.table.get_selected_row()
        if not row:
            self.log.append("[!] Выберите объявление в таблице")
            return None
        title = row[0].lstrip("▶ ")
        for ad in self._ads:
            if (ad.title or ad.text_base[:30]) == title:
                return ad
        return None

    def _add(self):
        dlg = AdDialog(self, self.app)
        self.wait_window(dlg)
        if dlg.result:
            ad, group_ids = dlg.result
            db = AdsDB(self.app.config.db_path)
            ad_id = db.add_ad(ad)
            for gid in group_ids:
                db.link_ad_to_group(ad_id, gid)
            db.close()
            self.log.append(f"[+] Добавлено объявление: {ad.title}")
            self.refresh()

    def _edit(self):
        ad = self._selected_ad()
        if not ad:
            return
        dlg = AdDialog(self, self.app, ad=ad)
        self.wait_window(dlg)
        if dlg.result:
            updated_ad, group_ids = dlg.result
            db = AdsDB(self.app.config.db_path)
            db.update_ad(updated_ad)
            # Обновляем связи с группами
            existing = {g.id for g in db.get_groups_for_ad(ad.id)}
            new_set = set(group_ids)
            for gid in new_set - existing:
                db.link_ad_to_group(ad.id, gid)
            for gid in existing - new_set:
                db.unlink_ad_from_group(ad.id, gid)
            db.close()
            self.log.append(f"[~] Обновлено: {updated_ad.title}")
            self.refresh()

    def _toggle(self):
        ad = self._selected_ad()
        if not ad:
            return
        ad.active = not ad.active
        db = AdsDB(self.app.config.db_path)
        db.update_ad(ad)
        db.close()
        state = "активно" if ad.active else "приостановлено"
        self.log.append(f"[~] {ad.title} → {state}")
        self.refresh()

    def _delete(self):
        ad = self._selected_ad()
        if not ad:
            return
        db = AdsDB(self.app.config.db_path)
        db.delete_ad(ad.id)
        db.close()
        self.log.append(f"[-] Удалено: {ad.title}")
        self.refresh()


# ─── Вкладка: Планировщик ────────────────────────────────────────────────────

class SchedulerTab(ctk.CTkFrame):
    """Вкладка управления планировщиком."""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self._scheduler = None
        _, LogFrame = _get_gui_widgets()

        # Статус + управление
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.pack(padx=10, pady=12, fill="x")

        self.lbl_status = ctk.CTkLabel(
            ctrl, text="⏹ Остановлен",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color="gray60")
        self.lbl_status.pack(side="left", padx=(0, 20))

        self.btn_start = ctk.CTkButton(
            ctrl, text="▶ Запустить", width=130,
            fg_color="green4", hover_color="green3",
            command=self._start)
        self.btn_start.pack(side="left", padx=5)

        self.btn_stop = ctk.CTkButton(
            ctrl, text="⏹ Остановить", width=130,
            fg_color="firebrick", hover_color="darkred",
            state="disabled", command=self._stop)
        self.btn_stop.pack(side="left", padx=5)

        # Настройки
        ctk.CTkLabel(self, text="Настройки планировщика",
                      font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=10, pady=(10, 5), anchor="w")

        settings_frame = ctk.CTkFrame(self)
        settings_frame.pack(padx=10, pady=5, fill="x")
        settings_frame.grid_columnconfigure(1, weight=0)
        settings_frame.grid_columnconfigure(3, weight=0)
        settings_frame.grid_columnconfigure(5, weight=1)

        def srow(label, placeholder, row, col=0):
            """Одиночное поле: Label + Entry"""
            ctk.CTkLabel(settings_frame, text=label, anchor="w").grid(
                row=row, column=col, padx=8, pady=5, sticky="w")
            e = ctk.CTkEntry(settings_frame, placeholder_text=placeholder, width=100)
            e.grid(row=row, column=col + 1, padx=8, pady=5, sticky="w")
            return e

        def srow_range(label, ph_min, ph_max, row):
            """Пара min/max: Label + 'мин' + Entry + 'макс' + Entry.
            Возвращает кортеж (entry_min, entry_max)."""
            ctk.CTkLabel(settings_frame, text=label, anchor="w").grid(
                row=row, column=0, padx=8, pady=5, sticky="w")
            ctk.CTkLabel(settings_frame, text="мин:",
                         font=ctk.CTkFont(size=11)).grid(
                row=row, column=1, padx=(12, 2), pady=5, sticky="e")
            e_min = ctk.CTkEntry(settings_frame, placeholder_text=ph_min, width=80)
            e_min.grid(row=row, column=2, padx=(0, 8), pady=5, sticky="w")
            ctk.CTkLabel(settings_frame, text="макс:",
                         font=ctk.CTkFont(size=11)).grid(
                row=row, column=3, padx=(8, 2), pady=5, sticky="e")
            e_max = ctk.CTkEntry(settings_frame, placeholder_text=ph_max, width=80)
            e_max.grid(row=row, column=4, padx=(0, 8), pady=5, sticky="w")
            return e_min, e_max

        def sheader(text, row):
            """Подзаголовок для группы настроек."""
            ctk.CTkLabel(settings_frame, text=text,
                         font=ctk.CTkFont(size=12, weight="bold"),
                         text_color="gray75").grid(
                row=row, column=0, columnspan=5, padx=8,
                pady=(8, 2), sticky="w")

        # ─── Планировщик объявлений ───
        sheader("Планировщик объявлений (рассылка рекламы):", 0)
        self.e_pub_min, self.e_pub_max = srow_range(
            "Интервал между публикациями (сек):", "300", "600", 1)
        self.e_daily_limit = srow("Лимит публикаций в сутки:", "30", 2, 0)
        self.e_join_min, self.e_join_max = srow_range(
            "Интервал между вступлениями в подписки (сек):", "900", "1800", 3)
        self.e_join_limit = srow("Лимит вступлений в сутки:", "5", 4, 0)

        # ─── Задержки рассылок и упоминаний ───
        sheader("Задержки между сообщениями (рандом из диапазона):", 5)
        self.e_broadcast_min, self.e_broadcast_max = srow_range(
            "Между сообщениями рассылки (сек):", "30", "90", 6)
        self.e_mention_min, self.e_mention_max = srow_range(
            "Между упоминаниями (сек):", "45", "120", 7)
        self.e_dm_min, self.e_dm_max = srow_range(
            "Между личными сообщениями (DM, сек):", "60", "180", 8)
        self.e_group_check_min, self.e_group_check_max = srow_range(
            "Между вступлениями при «Проверить и очистить» (сек):",
            "15", "45", 9)

        # AI настройки
        ctk.CTkLabel(self, text="AI настройки",
                      font=ctk.CTkFont(size=14, weight="bold")).pack(
            padx=10, pady=(12, 5), anchor="w")

        ai_frame = ctk.CTkFrame(self)
        ai_frame.pack(padx=10, pady=5, fill="x")
        ai_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(ai_frame, text="Провайдер:").grid(
            row=0, column=0, padx=8, pady=5, sticky="w")
        self.provider_var = ctk.StringVar(value="openai")
        ctk.CTkSegmentedButton(
            ai_frame, values=["openai", "groq"],
            variable=self.provider_var).grid(
            row=0, column=1, padx=8, pady=5, sticky="w")

        ctk.CTkLabel(ai_frame, text="OpenAI API Key:").grid(
            row=1, column=0, padx=8, pady=5, sticky="w")
        self.e_openai_key = ctk.CTkEntry(ai_frame, width=320, show="*")
        self.e_openai_key.grid(row=1, column=1, padx=8, pady=5, sticky="ew")

        ctk.CTkLabel(ai_frame, text="Groq API Key:").grid(
            row=2, column=0, padx=8, pady=5, sticky="w")
        self.e_groq_key = ctk.CTkEntry(ai_frame, width=320, show="*")
        self.e_groq_key.grid(row=2, column=1, padx=8, pady=5, sticky="ew")

        ctk.CTkButton(self, text="💾 Сохранить настройки", width=180,
                       command=self._save_settings).pack(padx=10, pady=8, anchor="w")

        # Лог
        ctk.CTkLabel(self, text="Лог планировщика:",
                      font=ctk.CTkFont(size=13)).pack(padx=10, anchor="w")
        self.log = LogFrame(self, height=150)
        self.log.pack(padx=10, pady=(3, 10), fill="x")

        self._load_settings()

    def _load_settings(self):
        db = AdsDB(self.app.config.db_path)
        s = db.load_scheduler_settings()
        db.close()

        # Планировщик — пары min/max + дневные лимиты
        self.e_pub_min.insert(0, str(s.publication_interval_min_seconds))
        self.e_pub_max.insert(0, str(s.publication_interval_max_seconds))
        self.e_daily_limit.insert(0, str(s.daily_publication_limit))
        self.e_join_min.insert(0, str(s.join_interval_min_seconds))
        self.e_join_max.insert(0, str(s.join_interval_max_seconds))
        self.e_join_limit.insert(0, str(s.daily_join_limit))

        # Задержки рассылок/упоминаний/DM/group-check
        self.e_broadcast_min.insert(0, str(s.broadcast_delay_min_seconds))
        self.e_broadcast_max.insert(0, str(s.broadcast_delay_max_seconds))
        self.e_mention_min.insert(0, str(s.mention_delay_min_seconds))
        self.e_mention_max.insert(0, str(s.mention_delay_max_seconds))
        self.e_dm_min.insert(0, str(s.dm_delay_min_seconds))
        self.e_dm_max.insert(0, str(s.dm_delay_max_seconds))
        self.e_group_check_min.insert(0, str(s.group_check_join_delay_min_seconds))
        self.e_group_check_max.insert(0, str(s.group_check_join_delay_max_seconds))

        self.provider_var.set(s.ai_provider)
        # API ключи берём из конфига если есть
        openai_key = getattr(self.app.config, "openai_api_key", "")
        groq_key = getattr(self.app.config, "groq_api_key", "")
        if openai_key:
            self.e_openai_key.insert(0, openai_key)
        if groq_key:
            self.e_groq_key.insert(0, groq_key)

    def _save_settings(self):
        def _int(widget, default):
            """Безопасно достать int из Entry, с fallback на default."""
            return int(widget.get() or str(default))

        try:
            s = SchedulerSettings(
                # Планировщик — пары min/max + дневные лимиты
                publication_interval_min_seconds=_int(self.e_pub_min, 300),
                publication_interval_max_seconds=_int(self.e_pub_max, 600),
                daily_publication_limit=_int(self.e_daily_limit, 30),
                join_interval_min_seconds=_int(self.e_join_min, 900),
                join_interval_max_seconds=_int(self.e_join_max, 1800),
                daily_join_limit=_int(self.e_join_limit, 5),

                # Broadcast / mention / DM / group-check
                broadcast_delay_min_seconds=_int(self.e_broadcast_min, 30),
                broadcast_delay_max_seconds=_int(self.e_broadcast_max, 90),
                mention_delay_min_seconds=_int(self.e_mention_min, 45),
                mention_delay_max_seconds=_int(self.e_mention_max, 120),
                dm_delay_min_seconds=_int(self.e_dm_min, 60),
                dm_delay_max_seconds=_int(self.e_dm_max, 180),
                group_check_join_delay_min_seconds=_int(self.e_group_check_min, 15),
                group_check_join_delay_max_seconds=_int(self.e_group_check_max, 45),

                # Legacy для обратной совместимости — выставляем = _min, чтобы сохранялись
                publication_interval_seconds=_int(self.e_pub_min, 300),
                join_interval_seconds=_int(self.e_join_min, 900),

                ai_provider=self.provider_var.get(),
            )
            s = clamp_settings(s)
            db = AdsDB(self.app.config.db_path)
            db.save_scheduler_settings(s)
            db.close()
            # Сохраняем API ключи в конфиг И в .env (чтобы не пропали при рестарте)
            openai_key = self.e_openai_key.get().strip()
            groq_key = self.e_groq_key.get().strip()
            import gui as _gui
            if openai_key:
                self.app.config.openai_api_key = openai_key
                _gui._update_env_file("OPENAI_API_KEY", openai_key)
            if groq_key:
                self.app.config.groq_api_key = groq_key
                _gui._update_env_file("GROQ_API_KEY", groq_key)
            self.log.append("[+] Настройки сохранены")
        except ValueError as e:
            self.log.append(f"[-] Ошибка: {e}")

    def _start(self):
        if self._scheduler and self._scheduler.is_running:
            return
        with _ADS_SCHEDULERS_GUARD:
            _prune_ads_schedulers_locked()

        from database import Database
        main_db = Database(self.app.config.db_path)
        accounts = [a for a in main_db.get_all_accounts() if a.is_active]
        main_db.close()

        if not accounts:
            self.log.append("[!] Нет активных аккаунтов")
            return

        account = accounts[0]
        with _ADS_SCHEDULERS_GUARD:
            if account.phone in _ADS_RUNNING_SCHEDULERS:
                self.log.append(f"[!] Уже запущено для аккаунта: {account.phone}")
                return

        def client_factory():
            """Возвращает НЕподключённый TelegramClient.
            Планировщик сам делает connect() и проверяет is_user_authorized()."""
            from sender import TelegramSender
            from database import Database as _Db
            # Создаём TelegramSender только для создания клиента с proxy+fingerprint.
            # Передаём фиктивный db — sender.client не использует его при создании.
            _db = _Db(self.app.config.db_path)
            try:
                sender = TelegramSender(account, self.app.config, _db)
                return sender._create_client()
            finally:
                _db.close()

        self._scheduler = AdsScheduler(
            db_path=self.app.config.db_path,
            account_phone=account.phone,
            client_factory=client_factory,
            log_cb=lambda msg: self.after(0, lambda m=msg: self.log.append(m)),
            tick_interval=60,
        )
        with _ADS_SCHEDULERS_GUARD:
            _ADS_RUNNING_SCHEDULERS[account.phone] = self._scheduler
        self._scheduler.start()
        self.lbl_status.configure(
            text="▶ Работает", text_color="green4")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

    def _stop(self):
        scheduler = self._scheduler
        if scheduler is None:
            with _ADS_SCHEDULERS_GUARD:
                _prune_ads_schedulers_locked()
                if len(_ADS_RUNNING_SCHEDULERS) == 1:
                    scheduler = next(iter(_ADS_RUNNING_SCHEDULERS.values()))
        stopped = True
        if scheduler:
            stopped = scheduler.stop()
            if stopped:
                with _ADS_SCHEDULERS_GUARD:
                    if _ADS_RUNNING_SCHEDULERS.get(scheduler.account_phone) is scheduler:
                        _ADS_RUNNING_SCHEDULERS.pop(scheduler.account_phone, None)
                if self._scheduler is scheduler:
                    self._scheduler = None
        if stopped:
            self.lbl_status.configure(text="⏹ Остановлен", text_color="gray60")
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
        else:
            self.lbl_status.configure(text="⏳ Останавливается", text_color="orange")
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")


# ─── Вкладка: История ────────────────────────────────────────────────────────

class HistoryTab(ctk.CTkFrame):
    """Вкладка журнала публикаций."""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        ScrollableTable, _ = _get_gui_widgets()

        # Фильтры
        flt = ctk.CTkFrame(self, fg_color="transparent")
        flt.pack(padx=10, pady=8, fill="x")

        ctk.CTkLabel(flt, text="Статус:").pack(side="left", padx=(0, 5))
        self.filter_status_var = ctk.StringVar(value="Все")
        ctk.CTkOptionMenu(
            flt, variable=self.filter_status_var,
            values=["Все", "ok", "flood_wait", "slow_mode",
                    "forbidden", "banned", "error"],
            width=130,
            command=lambda _: self.refresh(),
        ).pack(side="left", padx=5)

        ctk.CTkButton(flt, text="↻ Обновить", width=100,
                       command=self.refresh).pack(side="right", padx=5)

        # Таблица
        self.table = ScrollableTable(self, columns=[
            "Время", "Объявление", "Группа", "Аккаунт", "Статус", "Ошибка"])
        self.table.pack(padx=10, pady=5, fill="both", expand=True)

        self.refresh()

    def refresh(self):
        db = AdsDB(self.app.config.db_path)
        status_filter = self.filter_status_var.get()
        status = None if status_filter == "Все" else status_filter
        logs = db.get_publications_log(limit=200, status=status)

        # Строим карты для красивых названий
        ads = {a.id: a for a in db.get_all_ads()}
        groups = {g.id: g for g in db.get_all_groups()}
        db.close()

        rows = []
        for log in logs:
            ad_name = ads[log.ad_id].title if log.ad_id in ads else f"#{log.ad_id}"
            group_link = groups[log.group_id].link if log.group_id in groups else f"#{log.group_id}"
            time_str = log.time[11:16] if log.time and len(log.time) >= 16 else log.time
            rows.append((
                time_str,
                ad_name[:25],
                group_link,
                log.account_phone,
                log.status,
                (log.error_text or "")[:40],
            ))
        self.table.set_data(rows)


# ─── Быстрый запуск ──────────────────────────────────────────────────────────

class AccountsPickerDialog(ctk.CTkToplevel):
    def __init__(self, master, accounts: list, selected_phones: list[str]):
        super().__init__(master)
        self.result = None
        self._vars: dict[str, ctk.BooleanVar] = {}

        self.title("Аккаунты")
        self.geometry("480x560")
        self.resizable(False, True)
        self.grab_set()

        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=10)

        selected = set(selected_phones or [])
        phones = [a.phone for a in accounts if getattr(a, "is_active", False)]
        for p in phones:
            v = ctk.BooleanVar(value=(p in selected))
            self._vars[p] = v
            ctk.CTkCheckBox(frame, text=p, variable=v).pack(anchor="w", padx=10, pady=3)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(btns, text="OK", width=120, command=self._ok).pack(side="right", padx=5)
        ctk.CTkButton(btns, text="Отмена", width=120, fg_color="gray40",
                      hover_color="gray30", command=self._cancel).pack(side="right", padx=5)

    def _ok(self):
        self.result = [p for p, v in self._vars.items() if bool(v.get())]
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class GroupPickerDialog(ctk.CTkToplevel):
    def __init__(self, master, groups: list[GroupTarget], selected_ids: list[int]):
        super().__init__(master)
        self.result = None
        self._vars: dict[int, ctk.BooleanVar] = {}

        self.title("Цели (группы)")
        self.geometry("620x600")
        self.resizable(False, True)
        self.grab_set()

        frame = ctk.CTkScrollableFrame(self)
        frame.pack(fill="both", expand=True, padx=12, pady=10)

        selected = set(selected_ids or [])
        for g in groups:
            v = ctk.BooleanVar(value=(g.id in selected))
            self._vars[g.id] = v
            label = f"{g.link}" + (f" — {g.title}" if (g.title or "").strip() else "")
            ctk.CTkCheckBox(frame, text=label[:100], variable=v).pack(anchor="w", padx=10, pady=3)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkButton(btns, text="OK", width=120, command=self._ok).pack(side="right", padx=5)
        ctk.CTkButton(btns, text="Отмена", width=120, fg_color="gray40",
                      hover_color="gray30", command=self._cancel).pack(side="right", padx=5)

    def _ok(self):
        self.result = [gid for gid, v in self._vars.items() if bool(v.get())]
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


class QuickLaunchTab(ctk.CTkFrame):
    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app
        ScrollableTable, LogFrame = _get_gui_widgets()

        self._ads: list[Ad] = []
        self._groups: list[GroupTarget] = []
        self._ad_label_to_id: dict[str, int] = {}
        self._group_label_to_id: dict[str, int] = {}
        self._selected_accounts: list[str] = []
        self._schedulers: dict[str, AdsScheduler] = {}

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(padx=10, pady=(10, 0), fill="x")
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="1) Объявление:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.ad_var = ctk.StringVar(value="—")
        self.ad_menu = ctk.CTkOptionMenu(top, variable=self.ad_var, values=["—"], width=320,
                                         command=lambda _: self._on_ad_changed())
        self.ad_menu.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        ctk.CTkButton(top, text="Создать…", width=120, command=self._create_ad).grid(
            row=0, column=2, padx=5, pady=5, sticky="w"
        )
        ctk.CTkButton(top, text="Править…", width=120, command=self._edit_ad).grid(
            row=0, column=3, padx=5, pady=5, sticky="w"
        )

        ctk.CTkLabel(top, text="2) Цели:").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.lbl_targets = ctk.CTkLabel(top, text="—", text_color="gray70")
        self.lbl_targets.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        ctk.CTkButton(top, text="Выбрать…", width=120, command=self._pick_targets).grid(
            row=1, column=2, padx=5, pady=5, sticky="w"
        )
        ctk.CTkButton(top, text="Активировать", width=120, command=self._activate_targets).grid(
            row=1, column=3, padx=5, pady=5, sticky="w"
        )

        ctk.CTkLabel(top, text="3) Аккаунты:").grid(row=2, column=0, padx=5, pady=5, sticky="w")
        self.account_var = ctk.StringVar(value="—")
        self.account_menu = ctk.CTkOptionMenu(top, variable=self.account_var, values=["—"], width=200)
        self.account_menu.grid(row=2, column=1, padx=5, pady=5, sticky="w")
        ctk.CTkButton(top, text="Выбрать…", width=120, command=self._pick_accounts).grid(
            row=2, column=2, padx=5, pady=5, sticky="w"
        )
        self.multi_accounts_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(top, text="Запуск на нескольких", variable=self.multi_accounts_var,
                        command=self._refresh_accounts_ui).grid(
            row=2, column=3, padx=5, pady=5, sticky="w"
        )

        ctk.CTkLabel(top, text="4) Расписание:").grid(row=3, column=0, padx=5, pady=5, sticky="w")
        sch = ctk.CTkFrame(top, fg_color="transparent")
        sch.grid(row=3, column=1, padx=5, pady=5, sticky="w")
        ctk.CTkLabel(sch, text="Интервал (сек):").pack(side="left", padx=(0, 6))
        self.e_pub_min = ctk.CTkEntry(sch, width=80)
        self.e_pub_min.pack(side="left")
        ctk.CTkLabel(sch, text="..").pack(side="left", padx=6)
        self.e_pub_max = ctk.CTkEntry(sch, width=80)
        self.e_pub_max.pack(side="left")
        ctk.CTkLabel(sch, text="Лимит/сутки:").pack(side="left", padx=(12, 6))
        self.e_daily = ctk.CTkEntry(sch, width=80)
        self.e_daily.pack(side="left")
        ctk.CTkButton(top, text="Сохранить", width=120, command=self._save_schedule).grid(
            row=3, column=2, padx=5, pady=5, sticky="w"
        )

        ctk.CTkLabel(top, text="5) Предпросмотр:").grid(row=4, column=0, padx=5, pady=5, sticky="nw")
        pv = ctk.CTkFrame(top, fg_color="transparent")
        pv.grid(row=4, column=1, padx=5, pady=5, sticky="ew")
        pv.grid_columnconfigure(0, weight=1)
        self.preview_box = ctk.CTkTextbox(pv, height=110)
        self.preview_box.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(top, text="Обновить", width=120, command=self._preview).grid(
            row=4, column=2, padx=5, pady=5, sticky="w"
        )

        run = ctk.CTkFrame(self, fg_color="transparent")
        run.pack(padx=10, pady=10, fill="x")
        self.btn_start = ctk.CTkButton(run, text="▶ Запуск", width=140, command=self._start)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_stop = ctk.CTkButton(run, text="⏹ Стоп", width=140, state="disabled", command=self._stop)
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.lbl_status = ctk.CTkLabel(run, text="Статус: готов", text_color="gray70")
        self.lbl_status.pack(side="right")

        self.log = LogFrame(self)
        self.log.pack(padx=10, pady=(0, 10), fill="both", expand=True)

        self.refresh()

    def refresh(self):
        db = AdsDB(self.app.config.db_path)
        try:
            self._ads = db.get_all_ads()
            self._groups = db.get_all_groups()
            settings = db.load_scheduler_settings()
        finally:
            db.close()

        self._ad_label_to_id = {}
        ad_values = []
        for a in self._ads:
            label = f"#{a.id} • {(a.title or a.text_base[:30] or 'Без названия')[:40]}"
            self._ad_label_to_id[label] = int(a.id)
            ad_values.append(label)
        if not ad_values:
            ad_values = ["—"]
        cur = self.ad_var.get()
        self.ad_menu.configure(values=ad_values)
        if cur not in ad_values:
            self.ad_var.set(ad_values[0])

        from database import Database as MainDB
        main_db = MainDB(self.app.config.db_path)
        try:
            accounts = [a for a in main_db.get_all_accounts() if a.is_active]
        finally:
            main_db.close()
        phones = [a.phone for a in accounts]
        values = phones if phones else ["—"]
        self.account_menu.configure(values=values)
        if self.account_var.get() not in values:
            self.account_var.set(values[0])

        try:
            self.e_pub_min.delete(0, "end")
            self.e_pub_min.insert(0, str(settings.publication_interval_min_seconds))
            self.e_pub_max.delete(0, "end")
            self.e_pub_max.insert(0, str(settings.publication_interval_max_seconds))
            self.e_daily.delete(0, "end")
            self.e_daily.insert(0, str(settings.daily_publication_limit))
        except Exception:
            pass

        self._on_ad_changed()
        self._refresh_accounts_ui()
        self._preview()

    def _selected_ad(self):
        label = self.ad_var.get()
        ad_id = self._ad_label_to_id.get(label)
        if not ad_id:
            return None
        for a in self._ads:
            if int(a.id) == int(ad_id):
                return a
        return None

    def _linked_group_ids(self, ad_id: int) -> list[int]:
        db = AdsDB(self.app.config.db_path)
        try:
            groups = db.get_groups_for_ad(ad_id)
            return [int(g.id) for g in groups]
        finally:
            db.close()

    def _on_ad_changed(self):
        ad = self._selected_ad()
        if not ad:
            self.lbl_targets.configure(text="—")
            return
        group_ids = self._linked_group_ids(int(ad.id))
        self.lbl_targets.configure(text=f"{len(group_ids)} целей привязано")

    def _create_ad(self):
        dlg = AdDialog(self, self.app)
        self.wait_window(dlg)
        if not dlg.result:
            return
        ad, group_ids = dlg.result
        db = AdsDB(self.app.config.db_path)
        try:
            ad_id = db.add_ad(ad)
            for gid in group_ids:
                db.link_ad_to_group(ad_id, gid)
        finally:
            db.close()
        self.log.append(f"[+] Добавлено объявление: {ad.title}")
        self.refresh()

    def _edit_ad(self):
        ad = self._selected_ad()
        if not ad:
            return
        dlg = AdDialog(self, self.app, ad=ad)
        self.wait_window(dlg)
        if not dlg.result:
            return
        updated_ad, group_ids = dlg.result
        db = AdsDB(self.app.config.db_path)
        try:
            db.update_ad(updated_ad)
            existing = {g.id for g in db.get_groups_for_ad(ad.id)}
            new_set = set(group_ids)
            for gid in new_set - existing:
                db.link_ad_to_group(ad.id, gid)
            for gid in existing - new_set:
                db.unlink_ad_from_group(ad.id, gid)
        finally:
            db.close()
        self.log.append(f"[~] Обновлено: {updated_ad.title}")
        self.refresh()

    def _pick_targets(self):
        ad = self._selected_ad()
        if not ad:
            self.log.append("[!] Сначала выберите объявление")
            return
        current = self._linked_group_ids(int(ad.id))
        dlg = GroupPickerDialog(self, self._groups, current)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        selected = set(int(x) for x in dlg.result)
        db = AdsDB(self.app.config.db_path)
        try:
            existing = {int(g.id) for g in db.get_groups_for_ad(ad.id)}
            for gid in selected - existing:
                db.link_ad_to_group(ad.id, gid)
            for gid in existing - selected:
                db.unlink_ad_from_group(ad.id, gid)
        finally:
            db.close()
        self.log.append(f"[~] Цели обновлены: {len(selected)}")
        self._on_ad_changed()
        self._preview()

    def _activate_targets(self):
        ad = self._selected_ad()
        if not ad:
            self.log.append("[!] Сначала выберите объявление")
            return
        group_ids = self._linked_group_ids(int(ad.id))
        if not group_ids:
            self.log.append("[!] Нет выбранных целей")
            return
        db = AdsDB(self.app.config.db_path)
        try:
            for gid in group_ids:
                db.set_group_status(int(gid), GROUP_STATUS_ACTIVE)
        finally:
            db.close()
        self.log.append(f"[+] Активировано целей: {len(group_ids)}")

    def _pick_accounts(self):
        from database import Database as MainDB
        main_db = MainDB(self.app.config.db_path)
        try:
            accounts = main_db.get_all_accounts()
        finally:
            main_db.close()
        dlg = AccountsPickerDialog(self, accounts=accounts, selected_phones=self._selected_accounts)
        self.wait_window(dlg)
        if dlg.result is None:
            return
        self._selected_accounts = list(dlg.result or [])
        self.multi_accounts_var.set(True if self._selected_accounts else False)
        self._refresh_accounts_ui()

    def _refresh_accounts_ui(self):
        if bool(self.multi_accounts_var.get()):
            if self._selected_accounts:
                self.account_menu.configure(state="disabled")
                self.lbl_status.configure(text=f"Статус: выбранно аккаунтов {len(self._selected_accounts)}",
                                          text_color="gray70")
            else:
                self.account_menu.configure(state="disabled")
        else:
            self.account_menu.configure(state="normal")

    def _save_schedule(self):
        def _int(widget, default):
            return int(widget.get() or str(default))
        try:
            db = AdsDB(self.app.config.db_path)
            try:
                s = db.load_scheduler_settings()
                s.publication_interval_min_seconds = _int(self.e_pub_min, s.publication_interval_min_seconds)
                s.publication_interval_max_seconds = _int(self.e_pub_max, s.publication_interval_max_seconds)
                s.daily_publication_limit = _int(self.e_daily, s.daily_publication_limit)
                s.publication_interval_seconds = s.publication_interval_min_seconds
                s = clamp_settings(s)
                db.save_scheduler_settings(s)
            finally:
                db.close()
            self.log.append("[+] Расписание сохранено")
        except ValueError as e:
            self.log.append(f"[-] Ошибка: {e}")

    def _preview(self):
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        ad = self._selected_ad()
        if not ad:
            self.preview_box.insert("1.0", "Выберите объявление")
            self.preview_box.configure(state="disabled")
            return

        group_ids = self._linked_group_ids(int(ad.id))
        if not group_ids:
            self.preview_box.insert("1.0", "Выберите цели (группы) для объявления")
            self.preview_box.configure(state="disabled")
            return

        gid = int(group_ids[0])
        db = AdsDB(self.app.config.db_path)
        try:
            g = db.get_group(gid)
            adaptation = db.get_adaptation(int(ad.id), gid)
        finally:
            db.close()

        text = (adaptation.text if adaptation else ad.text_base) or ""
        head = f"Группа: {g.link if g else gid}\n"
        head += f"Медиа: {'да' if (ad.media_path and os.path.exists(ad.media_path)) else 'нет'}\n\n"
        if ad.button_text and ad.button_url:
            head += f"Кнопка: {ad.button_text} → {ad.button_url}\n\n"
        self.preview_box.insert("1.0", head + text)
        self.preview_box.configure(state="disabled")

    def _start(self):
        if self._schedulers:
            return
        ad = self._selected_ad()
        if not ad:
            self.log.append("[!] Выберите объявление")
            return
        group_ids = self._linked_group_ids(int(ad.id))
        if not group_ids:
            self.log.append("[!] Выберите цели")
            return

        if bool(self.multi_accounts_var.get()):
            phones = list(self._selected_accounts or [])
        else:
            phones = [self.account_var.get()] if (self.account_var.get() or "").strip() and self.account_var.get() != "—" else []

        if not phones:
            self.log.append("[!] Выберите аккаунт(ы)")
            return

        self._save_schedule()

        db = AdsDB(self.app.config.db_path)
        try:
            base_ad = db.get_ad(int(ad.id))
            if not base_ad:
                self.log.append("[!] Объявление не найдено")
                return

            for gid in group_ids:
                db.set_group_status(int(gid), GROUP_STATUS_ACTIVE)

            ad_ids_by_phone: dict[str, int] = {}
            if len(phones) == 1:
                base_ad.account_phone = phones[0]
                base_ad.active = True
                db.update_ad(base_ad)
                ad_ids_by_phone[phones[0]] = int(base_ad.id)
            else:
                base_ad.active = False
                db.update_ad(base_ad)
                for p in phones:
                    copy = Ad(
                        title=(base_ad.title or "Объявление") + f" [{p}]",
                        text_base=base_ad.text_base,
                        media_path=base_ad.media_path,
                        category=base_ad.category,
                        button_text=base_ad.button_text,
                        button_url=base_ad.button_url,
                        active=True,
                        account_phone=p,
                    )
                    new_id = db.add_ad(copy)
                    for gid in group_ids:
                        db.link_ad_to_group(new_id, int(gid))
                    ad_ids_by_phone[p] = int(new_id)
        finally:
            db.close()

        from database import Database as MainDB
        main_db = MainDB(self.app.config.db_path)
        try:
            accounts = {a.phone: a for a in main_db.get_all_accounts() if a.is_active}
        finally:
            main_db.close()

        started = 0
        with _ADS_SCHEDULERS_GUARD:
            _prune_ads_schedulers_locked()
            for p in phones:
                if p in _ADS_RUNNING_SCHEDULERS:
                    self.log.append(f"[!] Планировщик уже запущен для аккаунта {p}")
                    continue
                account = accounts.get(p)
                if not account:
                    self.log.append(f"[!] Аккаунт не найден/неактивен: {p}")
                    continue

                def client_factory(phone=p, acc=account):
                    from sender import TelegramSender
                    from database import Database as _Db
                    _db = _Db(self.app.config.db_path)
                    try:
                        sender = TelegramSender(acc, self.app.config, _db)
                        return sender._create_client()
                    finally:
                        _db.close()

                sch = AdsScheduler(
                    db_path=self.app.config.db_path,
                    account_phone=p,
                    client_factory=client_factory,
                    log_cb=lambda msg, _p=p: self.after(0, lambda m=msg, pp=_p: self.log.append(f"[{pp}] {m}")),
                    tick_interval=60,
                )
                _ADS_RUNNING_SCHEDULERS[p] = sch
                self._schedulers[p] = sch
                sch.start()
                started += 1

        if started:
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.lbl_status.configure(text=f"Статус: работает ({started})", text_color="green4")
        else:
            self.log.append("[!] Не удалось запустить ни одного планировщика")

    def _stop(self):
        with _ADS_SCHEDULERS_GUARD:
            _prune_ads_schedulers_locked()
            schedulers = dict(self._schedulers)
            if not schedulers:
                schedulers = dict(_ADS_RUNNING_SCHEDULERS)
        if not schedulers:
            return

        still_running = {}
        for p, sch in schedulers.items():
            try:
                stopped = sch.stop()
            except Exception:
                stopped = False
            if stopped:
                with _ADS_SCHEDULERS_GUARD:
                    if _ADS_RUNNING_SCHEDULERS.get(p) is sch:
                        _ADS_RUNNING_SCHEDULERS.pop(p, None)
            else:
                still_running[p] = sch

        self._schedulers = still_running
        if still_running:
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
            self.lbl_status.configure(text=f"Статус: останавливается ({len(still_running)})", text_color="orange")
        else:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.lbl_status.configure(text="Статус: остановлен", text_color="gray70")


# ─── Главный фрейм ───────────────────────────────────────────────────────────

class AdsMainFrame(ctk.CTkFrame):
    """
    Главный фрейм планировщика объявлений.
    Регистрируется в TeletonApp как "ads" в nav_items и _frame_classes.
    """

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self.app = app

        # Заголовок
        ctk.CTkLabel(self, text="Объявления",
                      font=ctk.CTkFont(size=20, weight="bold")).pack(
            padx=20, pady=(15, 5), anchor="w")

        # CTkTabview с 4 вкладками
        self.tabview = ctk.CTkTabview(self)
        self.tabview.pack(padx=20, pady=5, fill="both", expand=True)

        self.tab_launch = self.tabview.add("Запуск")
        self.tab_groups = self.tabview.add("Группы")
        self.tab_ads = self.tabview.add("Объявления")
        self.tab_scheduler = self.tabview.add("Планировщик")
        self.tab_history = self.tabview.add("История")

        # Создаём дочерние фреймы
        self.launch_tab = QuickLaunchTab(self.tab_launch, app)
        self.launch_tab.pack(fill="both", expand=True)

        self.groups_tab = GroupsTab(self.tab_groups, app)
        self.groups_tab.pack(fill="both", expand=True)

        self.ads_tab = AdsTab(self.tab_ads, app)
        self.ads_tab.pack(fill="both", expand=True)

        self.scheduler_tab = SchedulerTab(self.tab_scheduler, app)
        self.scheduler_tab.pack(fill="both", expand=True)

        self.history_tab = HistoryTab(self.tab_history, app)
        self.history_tab.pack(fill="both", expand=True)

    def on_show(self):
        """Вызывается при навигации на этот фрейм."""
        self.launch_tab.refresh()
        self.groups_tab.refresh()
        self.ads_tab.refresh()
        self.history_tab.refresh()
