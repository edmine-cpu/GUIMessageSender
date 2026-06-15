You are editing a Windows Python app in this folder.

Task: fix only the misleading/broken "Запустить все" behavior in gui.py.

Problem:
- In BroadcastFrame._mass_start_everything(), the code calls _cycle_start_enabled_campaigns() and immediately appends "циклы (все enabled)" to the started list.
- But _cycle_start_enabled_campaigns() only resumes already enabled cycle campaigns.
- If all campaigns are disabled, nothing starts, yet UI says mass start ran cycles.

Required minimal fix:
1. Modify only BroadcastFrame._mass_start_everything in gui.py if possible.
2. Before adding "циклы" to started, check whether at least one enabled cycle campaign with targets exists OR whether the call actually resulted in a running cycle worker.
3. If there are no enabled campaigns with targets, log a clear Russian message like:
   "[i] Циклы не запущены: нет включённых кампаний с целями. Выберите кампанию и нажмите обычный «Старт»."
4. Do NOT automatically enable disabled campaigns.
5. Do NOT change Telegram sending logic, DB schema, accounts, targets, parser, comments, TData import, or campaign stop logic.
6. Preserve single campaign Start behavior.
7. Keep the patch small and easy to review.

Verification commands:
py -3.12 -m py_compile gui.py database.py models.py sender.py
py -3.12 -m pytest tests\test_cycle_campaigns.py tests\test_cycle_start_regression.py -q

After editing, summarize exactly what changed and test results.
