import glob, os, datetime
logs = sorted(glob.glob("data/logs/teleton_*.log"), key=os.path.getmtime, reverse=True)
if logs:
    latest = logs[0]
    print("Latest log:", latest, "mtime:", datetime.datetime.fromtimestamp(os.path.getmtime(latest)))
    with open(latest, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    # Last 100 lines
    tail = lines[-100:]
    print("\n=== LAST 100 LINES OF LATEST LOG ===")
    print("".join(tail))
    # Filter for cyclic related
    cyclic = [l for l in lines if "[Циклическая]" in l or "cycle" in l.lower() or "БРИДЖ" in l or "АЛИСа" in l or "АЛИСА" in l]
    print("\n=== CYCLIC / CAMPAIGN RELATED ENTRIES (last 50) ===")
    for l in cyclic[-50:]:
        print(l.rstrip())
else:
    print("No logs found")
