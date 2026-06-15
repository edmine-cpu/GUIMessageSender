import sqlite3
import re
from collections import defaultdict
from datetime import datetime

print("=== ОТЧЁТ: Успешные отправки (phone -> target) ===\n")

# 1. From recent log (most accurate for "when")
log_path = "data/logs/teleton_2026-06-08.log"
recent_sends = []
try:
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-200:]
    current_phone = None
    for line in lines:
        if "[+]" in line and "+" in line and re.search(r"\+\d{10,}", line):
            m = re.search(r"(\+\d{10,})", line)
            if m:
                current_phone = m.group(1)
        if current_phone and "https://t.me/" in line and "[+]" in line:
            m = re.search(r"(https://t\.me/\S+)", line)
            if m:
                target = m.group(1)
                time_match = re.search(r"(\d{2}:\d{2}:\d{2})", line)
                t = time_match.group(1) if time_match else "??:??:??"
                recent_sends.append((t, current_phone, target))
                current_phone = None
except Exception as e:
    print("Log parse error:", e)

print("Последние успешные отправки (из лога):")
print("-" * 80)
for t, phone, target in recent_sends[-15:]:
    print(f"{t} | {phone} -> {target}")
print()

# 2. From DB - sent targets + dedicated phone per campaign
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row

print("\n=== Sent targets по кампаниям (из БД) ===")
for c in conn.execute("SELECT name FROM cycle_campaigns ORDER BY name"):
    cname = c["name"]
    print(f"\n{cname}:")
    rows = conn.execute("""
        SELECT t.link, t.status 
        FROM cycle_targets t 
        WHERE t.campaign_name = ? AND t.status = 'sent'
        ORDER BY t.id DESC LIMIT 5
    """, (cname,)).fetchall()
    if rows:
        for r in rows:
            print(f"  sent -> {r['link'][:60]}")
    else:
        print("  нет отправленных в последних записях")

# Map campaign -> dedicated phone
print("\n=== Dedicated phones per campaign ===")
for row in conn.execute("""
    SELECT c.name, a.phone, a.custom_name
    FROM cycle_campaigns c
    JOIN cycle_campaign_accounts ca ON ca.campaign_id = c.id
    JOIN accounts a ON a.id = ca.account_id
"""):
    name = row["custom_name"] or row["phone"]
    print(f"{row['name']}: {name} ({row['phone']})")

conn.close()
print("\n=== Конец отчёта ===")
