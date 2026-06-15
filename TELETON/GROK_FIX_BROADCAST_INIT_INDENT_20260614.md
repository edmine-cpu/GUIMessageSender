Ты работаешь в проекте C:\Users\Administrator\Desktop\TELETON_NEW_RUN.

Проблема после прошлой правки:
кнопка "Старт" / циклическая рассылка перестала нормально запускаться после добавления "запустить все".
Я нашел конкретный баг в gui.py: в классе BroadcastFrame блок инициализации общего лога и стоп-кнопки оказался внутри метода _mass_stop_everything(), а не в __init__.

Симптом:
- BroadcastFrame.__init__ строит табы, но self.log и btn_stop_current не создаются сразу.
- _mass_start_everything(), _cycle_start_enabled_campaigns(), _start_cycle() и on_show могут писать в self.log до создания self.log.
- В старом логе уже была ошибка: AttributeError: 'BroadcastFrame' object has no attribute 'log'.

Что нужно сделать строго минимально:

1. В gui.py, class BroadcastFrame:
   - перенеси создание общего лога:
     self.log = LogFrame(self, height=180)
     self.log.pack(...)
     self.btn_stop_current = ctk.CTkButton(...)
     self.btn_stop_current.pack(...)
     self.after(10000, self._cycle_watchdog)
     в конец BroadcastFrame.__init__, сразу после _build_* tab calls and helper setup.

2. Убери этот же блок из _mass_stop_everything().
   _mass_stop_everything должен только останавливать процессы, а не создавать UI-виджеты и не запускать watchdog.

3. В _mass_start_everything, _mass_stop_everything, _cycle_start_enabled_campaigns:
   - для логов используй self._append_log(...) вместо прямого self.log.append(...), если это простой текст.
   - либо гарантируй, что self.log уже создан до вызова методов.
   - Не меняй поведение рассылки, аккаунтов, лимитов, задержек и Telegram-логики.

4. Сохрани кнопку "запустить все", но она не должна ломать одиночный старт.
   - Одиночный "Старт" циклической кампании должен работать независимо от "запустить все".
   - Если запуск невозможен, в UI/log должна быть понятная причина.

5. Не переписывай архитектуру.
   Не трогай sender.py/database.py/models.py, если не требуется именно для этого бага.
   Цель: маленький надежный diff.

Проверка:
1. py -3.12 -m py_compile gui.py
2. py -3.12 -m pytest tests\ -q
3. grep/inspect: в _mass_stop_everything не должно быть self.log = LogFrame и btn_stop_current = ctk.CTkButton.
4. grep/inspect: в BroadcastFrame.__init__ self.log создается до завершения __init__.

В конце дай короткий отчет:
- какие файлы изменил;
- где именно была причина;
- какие тесты прошли;
- какие риски остались.
