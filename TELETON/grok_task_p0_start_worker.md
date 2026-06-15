Work in C:\Users\Administrator\Desktop\TELETON_NEW_RUN.

Narrow P0 task only. Do not run real Telegram sending.

Problem:
The cyclic broadcast UI can show "running"/disable Start, but no worker is alive and Sent does not grow. User also reports "Start all" broke normal single start.

Inspect only:
- gui.py cycle start/stop methods
- any "start all" method
- database.py campaign enabled/status methods

Find and patch only the smallest reliability bugs that explain:
1. campaign enabled=1 but no worker/thread alive;
2. worker startup exception leaves UI disabled;
3. Start all reuses global state and blocks single start;
4. Stop does not clear enabled state.

Required verification:
- py -3.12 -m py_compile gui.py database.py
- py -3.12 -m pytest tests\ -q
- Do not start real sending.

Report concise:
- root cause;
- files changed;
- tests output;
- exact safe GUI verification steps.
