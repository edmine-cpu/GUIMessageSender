import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== ЦИКЛИЧЕСКИЕ КАМПАНИИ ===")
for r in conn.execute("SELECT id, name, enabled, targets_source, message_source, template_id, current_position FROM cycle_campaigns ORDER BY name"):
    print(dict(r))
print()
print("=== ПРИВЯЗКА АККАУНТОВ К КАМПАНИЯМ ===")
for r in conn.execute("""
    SELECT c.id as cid, c.name as campaign, ca.account_phone, a.custom_name, a.proxy, a.is_active
    FROM cycle_campaigns c
    LEFT JOIN cycle_campaign_accounts ca ON ca.campaign_id = c.id
    LEFT JOIN accounts a ON a.phone = ca.account_phone
    ORDER BY c.name
"""):
    print(dict(r))
print()
print("=== ЦЕЛИ ПО КАМПАНИЯМ (счёт) ===")
for r in conn.execute("""
    SELECT ct.campaign_name, COUNT(*) as total, 
           SUM(CASE WHEN ct.status='active' THEN 1 ELSE 0 END) as active
    FROM cycle_targets ct
    GROUP BY ct.campaign_name
    ORDER BY ct.campaign_name
"""):
    print(dict(r))
print()
print("=== ДВА КЛЮЧЕВЫХ АККАУНТА ===")
for r in conn.execute("SELECT phone, custom_name, proxy, is_active, status, last_error_text, health FROM accounts WHERE phone IN ('+18023057895', '+905482809547')"):
    print(dict(r))
conn.close()
