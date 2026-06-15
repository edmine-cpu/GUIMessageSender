import sqlite3, json, os, glob, datetime
base = r'C:\Users\Administrator\Desktop\TELETON_NEW_RUN'
db = os.path.join(base, 'data', 'teleton.db')
print('DB_EXISTS', os.path.exists(db), db)
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
for table in ['cycle_campaigns','cycle_campaign_accounts','cycle_targets','accounts','settings']:
    print('SCHEMA', table)
    try:
        cols=[r['name'] for r in con.execute(f'pragma table_info({table})')]
        print(json.dumps(cols, ensure_ascii=False))
    except Exception as e:
        print('ERR', repr(e))
print('CAMPAIGNS')
try:
    for r in con.execute('select * from cycle_campaigns order by id'):
        print(json.dumps(dict(r), ensure_ascii=False))
except Exception as e: print('ERR', repr(e))
print('CAMPAIGN_ACCOUNTS')
try:
    for r in con.execute('select * from cycle_campaign_accounts order by campaign_id,phone'):
        print(json.dumps(dict(r), ensure_ascii=False))
except Exception as e: print('ERR', repr(e))
print('ACCOUNTS')
try:
    for r in con.execute('select phone,enabled,health,last_error_text,actions_today,error_today,paused_until,pause_reason from accounts order by phone'):
        print(json.dumps(dict(r), ensure_ascii=False))
except Exception as e: print('ERR', repr(e))
print('TARGET_COUNTS')
try:
    for r in con.execute('select campaign_id, count(*) total, sum(case when coalesce(last_error,"")<>"" then 1 else 0 end) errors from cycle_targets group by campaign_id order by campaign_id'):
        print(json.dumps(dict(r), ensure_ascii=False))
except Exception as e: print('ERR', repr(e))
print('LATEST_LOGS')
for p in sorted(glob.glob(os.path.join(base, 'data', 'logs', '*')), key=os.path.getmtime, reverse=True)[:8]:
    print(os.path.basename(p), datetime.datetime.fromtimestamp(os.path.getmtime(p)).isoformat(), os.path.getsize(p))
