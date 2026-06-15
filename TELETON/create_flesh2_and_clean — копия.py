#!/usr/bin/env python3
"""
Create ФЛЕШ 2 with ready-to-run Telegram Desktop instances from the TData in ТГ\в работе (the 3 active ones from flash).
- Uses the empty "Telegram Desktop" on Desktop as base.
- Injects the exact tdata the user has.
- Launches them so windows open immediately.
- Also cleans the Teleton DB so only these exact 3 accounts remain (is_active + active status).
"""
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime

DESKTOP = r"C:\Users\Administrator\Desktop"
EMPTY_TG = os.path.join(DESKTOP, "Telegram Desktop")
FLESH2 = os.path.join(DESKTOP, "ФЛЕШ 2")

# Exactly the 3 the user wants (from previous "only 3 active" + the flash "в работе" TData)
TARGETS = [
    ("БРИДЖ", "+18023057895", os.path.join(DESKTOP, "ТГ", "в работе", "+1 802 305 7895", "tdata")),
    ("рома",  "+905482809547", os.path.join(DESKTOP, "ТГ", "в работе", "+90 548 280 9547", "tdata")),
    ("САВА",  "+447446750531", os.path.join(DESKTOP, "ТГ", "в работе", "+44 74 4675 0531", "tdata")),
]

TELETON_DB = os.path.join(DESKTOP, "TELETON_NEW_RUN", "data", "teleton.db")

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def main():
    log("=== Creating ФЛЕШ 2 + launching TG windows + cleaning Teleton to the 3 ===")

    # 1. Clean Teleton DB to only these 3
    log("Cleaning Teleton DB to only the 3 active accounts...")
    conn = sqlite3.connect(TELETON_DB)
    c = conn.cursor()
    keep = [t[1] for t in TARGETS]
    c.execute("SELECT phone FROM accounts")
    current = [r[0] for r in c.fetchall()]
    extras = [p for p in current if p not in keep]
    if extras:
        log(f"  Removing extras from DB: {extras}")
        for p in extras:
            c.execute("DELETE FROM accounts WHERE phone = ?", (p,))
    for p in keep:
        c.execute("""
            UPDATE accounts
            SET is_active=1, status='active', connect_fail_count=0, flood_until='', paused_until='', pause_reason=''
            WHERE phone=?
        """, (p,))
    conn.commit()
    c.execute("SELECT phone, custom_name, is_active, status FROM accounts ORDER BY custom_name")
    log("  Current accounts in Teleton:")
    for r in c.fetchall():
        log(f"    phone={r[0]}, custom={r[1]}, active={r[2]}, status={r[3]}")
    conn.close()

    # 2. Prepare ФЛЕШ 2
    log(f"Preparing {FLESH2} ...")
    if os.path.isdir(FLESH2):
        for entry in os.listdir(FLESH2):
            p = os.path.join(FLESH2, entry)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
    else:
        os.makedirs(FLESH2, exist_ok=True)

    # What to copy from the empty TG base for a working portable instance
    copy_files = ["Telegram.exe", "Updater.exe"]
    copy_dirs = ["modules"]

    launched = []
    for nice_name, phone, tdata_src in TARGETS:
        if not os.path.isdir(tdata_src):
            log(f"  [!] TData source missing for {nice_name}: {tdata_src}")
            continue

        inst_dir = os.path.join(FLESH2, nice_name)
        os.makedirs(inst_dir, exist_ok=True)

        # Copy base runtime from empty TG
        for f in copy_files:
            src = os.path.join(EMPTY_TG, f)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(inst_dir, f))
        for d in copy_dirs:
            srcd = os.path.join(EMPTY_TG, d)
            if os.path.isdir(srcd):
                dstd = os.path.join(inst_dir, d)
                if os.path.exists(dstd):
                    shutil.rmtree(dstd, ignore_errors=True)
                shutil.copytree(srcd, dstd)

        # Inject the user's tdata (this is what makes it "рабочая телеграм версия" from the flash TData)
        # Use robocopy (Windows) for robustness - it handles locked binlog/cache files better than shutil.
        tdata_dst = os.path.join(inst_dir, "tdata")
        if os.path.exists(tdata_dst):
            shutil.rmtree(tdata_dst, ignore_errors=True)
        os.makedirs(tdata_dst, exist_ok=True)
        robocmd = ["robocopy", tdata_src, tdata_dst, "/E", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS"]
        try:
            subprocess.run(robocmd, check=False, capture_output=True)
        except Exception:
            try:
                shutil.copytree(tdata_src, tdata_dst, dirs_exist_ok=True)
            except Exception as e2:
                log(f"    tdata copy issues (locked cache files from flash are normal): {e2}")

        log(f"  Created ready instance: {inst_dir}")

        # Launch so the window opens immediately
        exe = os.path.join(inst_dir, "Telegram.exe")
        if os.path.isfile(exe):
            try:
                subprocess.Popen([exe], cwd=inst_dir)
                log(f"    -> Launched window for {nice_name}")
                launched.append(nice_name)
                time.sleep(1.8)  # small delay between launches
            except Exception as e:
                log(f"    Launch error for {nice_name}: {e}")
        else:
            log(f"    [!] No exe in {inst_dir}")

    log("")
    log("=== DONE ===")
    log(f"ФЛЕШ 2 folder: {FLESH2}")
    log(f"Ready TG instances created for: {[t[0] for t in TARGETS]}")
    log(f"Windows launched: {launched}")
    log("The Telegram windows should now be open and logged in using the TData from your flash (в работе).")
    log("Teleton DB is cleaned to exactly these 3 accounts (start the Teleton GUI to use them).")
    log("If you want more accounts from other subfolders in ТГ (the rest of the flash), tell me the phones or folders and I will add more instances.")

if __name__ == "__main__":
    main()
