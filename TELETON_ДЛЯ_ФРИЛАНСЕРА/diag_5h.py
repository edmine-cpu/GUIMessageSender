import sys
from datetime import datetime
from database import Database

print(f"=== TELETON DIAGNOSTIC REPORT @ {datetime.now().isoformat()} ===")
print()

try:
    db = Database('data/teleton.db')
    campaigns = db.get_all_cycle_campaigns()
    print("CYCLE CAMPAIGNS:")
    for c in campaigns:
        enabled = getattr(c, 'enabled', 'N/A')
        print(f"  - {c.name}: enabled={enabled}")
    print()

    print("ACCOUNTS HEALTH SUMMARY:")
    health = db.get_accounts_health()
    active = sum(1 for h in health if h.get('health') in ('active', ''))
    print(f"  Total accounts: {len(health)}, healthy-looking: {active}")
    for h in health[:5]:
        print(f"    {h['phone']}: {h.get('health')} | why={h.get('why','')[:40]}")
    print()

    print("SAMPLE TARGET STATUS (first enabled campaign):")
    for c in campaigns:
        if getattr(c, 'enabled', False):
            targets = db.get_cycle_targets(c.name, limit=3)
            for t in targets:
                print(f"  {t['link'][:45]}... -> status={t['status']} err={str(t.get('last_error',''))[:50]}")
            break
    db.close()
except Exception as e:
    print(f"DB ERROR: {e}")

print()
print("LOG TAIL (last errors/sends):")
try:
    with open('data/logs/teleton_2026-06-08.log', 'r', encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()[-30:]
        for line in lines:
            if any(kw in line for kw in ['ERROR', 'Exception', 'closed database', '[+]', '[x]', 'cycle']):
                print('  ' + line.strip()[:120])
except Exception as e:
    print(f"Log read error: {e}")

print()
print("=== END REPORT ===")
print("Note: Full 5h scheduler will produce richer version with fixes if issues found.")