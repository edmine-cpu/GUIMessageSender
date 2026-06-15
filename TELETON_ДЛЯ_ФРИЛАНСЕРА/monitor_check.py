import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== ВСЕ АККАУНТЫ (статус + прокси + ошибки) ===")
for r in conn.execute("SELECT phone, custom_name, proxy, is_active, status, last_error_text, error_today, actions_today, last_send_at FROM accounts ORDER BY custom_name, phone"):
    p = r["proxy"][:40] + "..." if r["proxy"] and len(r["proxy"])>40 else (r["proxy"] or "(empty)")
    err = (r["last_error_text"] or "-")[:50]
    last = r["last_send_at"][:19] if r["last_send_at"] else "never"
    print(f"{r['phone']} | {str(r['custom_name'] or '-').ljust(8)} | proxy:{p} | active:{r['is_active']} | errs_today:{r['error_today']} | actions:{r['actions_today']} | last:{last} | err:{err}")
print()
print("=== КАМПАНИИ + ПРИВЯЗКИ ===")
for r in conn.execute("""
    SELECT c.name, c.enabled, ca.account_phone, a.custom_name
    FROM cycle_campaigns c 
    LEFT JOIN cycle_campaign_accounts ca ON ca.campaign_id = c.id
    LEFT JOIN accounts a ON a.phone = ca.account_phone
    ORDER BY c.name
"""):
    print(dict(r))
print()
print("=== ЦЕЛЕЙ В КАМПАНИЯХ ===")
for r in conn.execute("""
    SELECT c.name, COUNT(ct.id) as targets
    FROM cycle_campaigns c
    LEFT JOIN cycle_targets ct ON ct.campaign_id = c.id
    GROUP BY c.id, c.name
    ORDER BY c.name
"""):
    print(dict(r))
conn.close()
