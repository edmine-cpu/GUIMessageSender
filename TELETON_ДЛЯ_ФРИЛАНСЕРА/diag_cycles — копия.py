import sqlite3
conn = sqlite3.connect("data/teleton.db")
conn.row_factory = sqlite3.Row
print("=== CAMPAIGNS ===")
for r in conn.execute("SELECT id,name,enabled FROM cycle_campaigns ORDER BY name"):
    print(dict(r))
print("=== ASSIGNMENTS FOR БРИДЖ/АЛИСа ===")
for r in conn.execute("""
SELECT c.name, ca.account_phone, a.custom_name, a.is_active 
FROM cycle_campaigns c 
JOIN cycle_campaign_accounts ca ON ca.campaign_id=c.id 
LEFT JOIN accounts a ON a.phone=ca.account_phone
WHERE c.name LIKE "%БРИДЖ%" OR c.name LIKE "%АЛИС%"
"""):
    print(dict(r))
print("=== TARGET COUNTS ===")
for r in conn.execute("SELECT campaign_name, COUNT(*) total FROM cycle_targets GROUP BY campaign_name"):
    print(dict(r))
conn.close()
