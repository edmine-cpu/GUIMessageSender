
from pathlib import Path
import sqlite3
root=Path(r'C:\Users\Administrator\Desktop\TELETON_NEW_RUN')
db=root/'data'/'teleton.db'
print('db exists', db.exists(), db.stat().st_size if db.exists() else '-')
con=sqlite3.connect(db)
con.row_factory=sqlite3.Row
cur=con.cursor()
print('tables:', [r[0] for r in cur.execute("select name from sqlite_master where type='table' order by name").fetchall()])
for table in ['cycle_campaigns','cycle_campaign_accounts','cycle_targets','cycle_state','accounts']:
    try:
        cols=[r[1] for r in cur.execute(f'pragma table_info({table})')]
        print('\nTABLE',table, cols)
        rows=cur.execute(f'select * from {table} limit 10').fetchall()
        for r in rows:
            d=dict(r)
            for k,v in list(d.items()):
                if isinstance(v,str) and len(v)>100:
                    d[k]=v[:100]+'...'
            print(d)
    except Exception as e:
        print('ERR',table,e)
con.close()
logs=sorted((root/'data'/'logs').glob('*.log'), key=lambda p:p.stat().st_mtime, reverse=True)[:5]
print('\nrecent_logs', [p.name for p in logs])
for p in logs[:2]:
    print('\n--',p.name,'--')
    txt=p.read_text(encoding='utf-8', errors='ignore')[-2500:]
    print(txt)
