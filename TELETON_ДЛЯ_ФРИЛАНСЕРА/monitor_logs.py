import glob, os
logs = sorted(glob.glob("data/logs/teleton_events_*.log"), key=os.path.getmtime, reverse=True)[:1]
if logs:
    with open(logs[0], "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    print("=== RECENT CYCLIC ACTIVITY (last 60 lines with cycle or key phones) ===")
    key_phones = ["+18023057895", "+905482809547", "+12513654685", "+14184681994", "+15313493507", "+17792952990"]
    relevant = [l.rstrip() for l in lines[-400:] if "cycle |" in l or any(ph in l for ph in key_phones)]
    for l in relevant[-60:]:
        print(l)
    print()
    print("=== LAST SUCCESSFUL SENDS (БРИДЖ / АЛИСА / new) ===")
    sent_lines = [l for l in lines if "send | sent" in l]
    for l in sent_lines[-10:]:
        print(l.rstrip())
else:
    print("No recent event logs")
