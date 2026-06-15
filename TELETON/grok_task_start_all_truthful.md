You are working in C:\Users\Administrator\Desktop\TELETON_NEW_RUN.

Task: fix ONLY the UI/control-flow bug around the BroadcastFrame "Запустить все" / _mass_start_everything behavior.

Observed facts from diagnostics:
- User clicked "Запустить все".
- Logs say:
  [МАССОВЫЙ ЗАПУСК] ...
  [Циклическая] [~] Ручной запуск всех включённых кампаний...
  [Циклическая] [i] Нет включённых кампаний с целями и аккаунтами.
  [🚀] Массово запущено: циклы (все enabled), упоминания.
- DB showed all cycle_campaigns enabled=0, so no cycles actually started.
- This is misleading and makes UI look broken.

Required fix:
1. _mass_start_everything must not append "циклы" to started unless at least one cycle campaign actually started or was already alive.
2. If no enabled cycle campaigns with targets exist, log a clear Russian message explaining that cycles were not started because no enabled campaigns are configured.
3. Do not enable disabled campaigns automatically. Do not change limits, delays, message source, accounts, targets, DB schema, or sending logic.
4. Do not touch parser/commenting/accounts/TData. No architecture rewrite.
5. Keep single campaign "Старт" behavior unchanged.
6. After patch, run:
   py -3.12 -m py_compile gui.py database.py models.py sender.py
   py -3.12 -m pytest tests\test_cycle_campaigns.py tests\test_cycle_start_regression.py -q

Return:
- exact files changed
- short explanation
- test output
- any remaining risk
