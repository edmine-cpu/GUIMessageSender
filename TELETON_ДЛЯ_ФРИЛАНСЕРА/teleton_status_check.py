import sqlite3, os, glob, time, json
base = r'C:\Users\Administrator\Desktop\TELETON_NEW_RUN'
db_path = os.path.join(base, 'data', 'teleton.db')
print('DB', db_path, 'exists', os.path.exists(db_path), 'size', os.path.getsize(db_path) if os.path.exists(db_path) else 0)
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
for table in ['accounts','cycle_campaigns','cycle_targets','send_logs','broadcast_tasks']:
    try:
        print('\nTABLE', table)
        print('cols:', [r['name'] for r in cur.execute(f'PRAGMA table_info({table})')])
        print('count:', cur.execute(f'SELECT COUNT(*) c FROM {table}').fetchone()['c'])
    except Exception as e:
        print('ERR', table, repr(e))
print('\nCAMPAIGNS')
for r in cur.execute('SELECT * FROM cycle_campaigns ORDER BY id DESC LIMIT 20'):
    print(dict(r))
print('\nTARGETS by campaign')
try:
    for r in cur.execute('SELECT campaign_id, COUNT(*) cnt, MIN(position) minpos, MAX(position) maxpos, SUM(CASE WHEN status="active" THEN 1 ELSE 0 END) active FROM cycle_targets GROUP BY campaign_id ORDER BY campaign_id'):
        print(dict(r))
except Exception as e: print('targets err', e)
print('\nACCOUNTS sample')
try:
    for r in cur.execute('SELECT * FROM accounts ORDER BY rowid DESC LIMIT 20'):
        d = dict(r); 
        for k in list(d):
            if k.lower() in ('proxy','session_path') and d[k]: d[k] = str(d[k])[:30]+'...'
        print(d)
except Exception as e: print('accounts err', e)
print('\nRECENT send_logs')
try:
    for r in cur.execute('SELECT * FROM send_logs ORDER BY rowid DESC LIMIT 15'):
        print(dict(r))
except Exception as e: print('send_logs err', e)
print('\nLOG FILES')
for p in sorted(glob.glob(os.path.join(base,'data','logs','*.log')), key=os.path.getmtime, reverse=True)[:8]:
    print(os.path.basename(p), time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(p))), os.path.getsize(p))
    try:
        with open(p,'r',encoding='utf-8',errors='replace') as f:
            lines=f.read().splitlines()[-20:]
        for line in lines[-8:]: print('  ', line[:500])
    except Exception as e: print('  read err', e)
conn.close()
