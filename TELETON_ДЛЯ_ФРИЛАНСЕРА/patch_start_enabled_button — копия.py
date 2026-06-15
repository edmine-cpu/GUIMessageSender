from pathlib import Path

path = Path(r"C:\Users\Administrator\Desktop\TELETON_NEW_RUN\gui.py")
text = path.read_text(encoding="utf-8")

if "def _cycle_start_enabled_campaigns(self):" not in text:
    marker = '''        ctk.CTkButton(btns, text="Правила чата", width=140,
                      command=self._cycle_edit_selected).pack(side="right")

'''
    insert = '''        ctk.CTkButton(btns, text="Правила чата", width=140,
                      command=self._cycle_edit_selected).pack(side="right")

        self.btn_cycle_start_enabled = ctk.CTkButton(
            btns,
            text="▶ Включённые",
            width=150,
            command=self._cycle_start_enabled_campaigns,
        )
        self.btn_cycle_start_enabled.pack(side="left", padx=(12, 0))

'''
    if marker not in text:
        raise SystemExit("button marker not found")
    text = text.replace(marker, insert, 1)

    old = '''        if busy:
            for btn_name in ("btn_cycle_start",):
'''
    new = '''        if busy:
            for btn_name in ("btn_cycle_start", "btn_cycle_start_enabled"):
'''
    if old not in text:
        raise SystemExit("busy marker not found")
    text = text.replace(old, new, 1)

    old = '''            self.btn_cycle_start.configure(state="disabled" if selected_running else "normal")
            self.btn_cycle_stop.configure(
'''
    new = '''            self.btn_cycle_start.configure(state="disabled" if selected_running else "normal")
            btn_start_enabled = getattr(self, "btn_cycle_start_enabled", None)
            if btn_start_enabled:
                btn_start_enabled.configure(
                    state="disabled" if getattr(self, "_cycle_ui_busy", False) else "normal"
                )
            self.btn_cycle_stop.configure(
'''
    if old not in text:
        raise SystemExit("refresh marker not found")
    text = text.replace(old, new, 1)

    marker = '''        except Exception:
            pass

    def _cycle_sync_targets_for_current_source(self) -> dict:
'''
    insert = '''        except Exception:
            pass

    def _cycle_start_enabled_campaigns(self):
        """Start saved enabled cycle campaigns that are not already running.

        This is the safe pair to the one-click stop: it does not change campaign
        limits, delays, targets, message text, or enable disabled campaigns.
        """
        if getattr(self, "_resume_in_progress", False):
            self.log.append("[Циклическая] [i] Запуск включённых кампаний уже выполняется.")
            return
        if getattr(self, "_cycle_ui_busy", False):
            self.log.append("[Циклическая] [i] Подождите завершения текущей операции интерфейса.")
            return
        self.log.append("[Циклическая] [~] Ручной запуск всех включённых кампаний...")
        self._resume_enabled_cycles(only_dead=True)

    def _cycle_sync_targets_for_current_source(self) -> dict:
'''
    if marker not in text:
        raise SystemExit("method marker not found")
    text = text.replace(marker, insert, 1)

    path.write_text(text, encoding="utf-8")
    print("patched")
else:
    print("already patched")
