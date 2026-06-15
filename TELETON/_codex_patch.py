
from pathlib import Path
import shutil, datetime, subprocess, sys
root=Path(r'C:\Users\Administrator\Desktop\TELETON_NEW_RUN')
main=root/'gui.py'
cand=root/'gui_candidate_copy.py'
text=cand.read_text(encoding='utf-8', errors='ignore')
old="""    def _cycle_runner_alive(self, runner) -> bool:\n        try:\n            thread = runner.get(\"thread\") if isinstance(runner, dict) else runner\n            return thread is not None and thread.is_alive()\n        except Exception:\n            return False\n"""
new="""    def _cycle_runner_alive(self, runner) -> bool:\n        try:\n            thread = runner.get(\"thread\") if isinstance(runner, dict) else runner\n            return thread is not None and thread.is_alive()\n        except Exception:\n            return False\n\n    def _cycle_selected_running(self, campaign_name: str | None = None) -> bool:\n        \"\"\"True only for the selected campaign, not for any campaign globally.\"\"\"\n        try:\n            name = (campaign_name or self._cycle_campaign_name or \"\").strip()\n            runner = self._cycle_get_runner(name) if name else None\n            if not self._cycle_runner_alive(runner):\n                if name and isinstance(getattr(self, \"_cycle_runners\", None), dict):\n                    self._cycle_runners.pop(name, None)\n                return False\n            return True\n        except Exception:\n            return False\n"""
if old in text and '_cycle_selected_running' not in text:
    text=text.replace(old,new,1)
else:
    print('runner helper insertion skipped', old in text, '_cycle_selected_running' in text)
text=text.replace("""            self._cycle_running = True\n            self.btn_cycle_start.configure(state=\"disabled\")\n            self.btn_cycle_stop.configure(state=\"normal\", text=\"■ Стоп\")\n""","""            self._cycle_running = True\n            if (self._cycle_campaign_name or \"\").strip() == running_campaign_name:\n                self.btn_cycle_start.configure(state=\"disabled\")\n                self.btn_cycle_stop.configure(state=\"normal\", text=\"■ Стоп\")\n""",1)
text=text.replace("""                self._cycle_running = False\n                if (getattr(self, \"_cycle_running_campaign_name\", \"\") or \"\") == running_campaign_name:\n                    self._cycle_running_campaign_name = \"\"\n                runners = getattr(self, \"_cycle_runners\", None) or {}\n                runners.pop(running_campaign_name, None)\n                self.btn_cycle_start.configure(state=\"normal\")\n                self.btn_cycle_stop.configure(state=\"disabled\", text=\"■ Стоп\")\n""","""                runners = getattr(self, \"_cycle_runners\", None) or {}\n                runners.pop(running_campaign_name, None)\n                self._cycle_running = bool(self._cycle_active_names())\n                if (getattr(self, \"_cycle_running_campaign_name\", \"\") or \"\") == running_campaign_name:\n                    self._cycle_running_campaign_name = (self._cycle_active_names() or [\"\"])[0]\n                if (self._cycle_campaign_name or \"\").strip() == running_campaign_name:\n                    self.btn_cycle_start.configure(state=\"normal\")\n                    self.btn_cycle_stop.configure(state=\"disabled\", text=\"■ Стоп\")\n""",1)
text=text.replace("""        selected_alive = self._cycle_runner_alive(self._cycle_get_runner(selected))\n""","""        selected_alive = self._cycle_selected_running(selected)\n""")
cand.write_text(text, encoding='utf-8')
r=subprocess.run([sys.executable,'-m','py_compile',str(cand)],capture_output=True,text=True)
print('candidate_compile', r.returncode)
if r.stderr:
    print(r.stderr[-2000:])
if r.returncode:
    raise SystemExit(r.returncode)
ts=datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
bak=root/f'gui.py.bak_before_mass_merge_{ts}'
shutil.copy2(main,bak)
shutil.copy2(cand,main)
r2=subprocess.run([sys.executable,'-m','py_compile',str(main)],capture_output=True,text=True)
print('main_compile', r2.returncode)
if r2.stderr:
    print(r2.stderr[-2000:])
print('backup', bak.name)
print('main_size', main.stat().st_size)
mt=main.read_text(encoding='utf-8', errors='ignore')
for needle in ['МАССОВОЕ УПРАВЛЕНИЕ','ЗАПУСТИТЬ ВСЁ','_cycle_start_enabled_campaigns','_cycle_selected_running']:
    print(needle, mt.find(needle))
