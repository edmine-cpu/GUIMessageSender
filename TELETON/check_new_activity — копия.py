import glob, os
logs = sorted(glob.glob("data/logs/teleton_events_*.log"), key=os.path.getmtime, reverse=True)[:1]
if logs:
    with open(logs[0], "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    new_phones = ["+12513654685", "+14184681994", "+15313493507", "+17792952990", "+447446750531"]
    new_camps = ["roma", "cawa", "water", "sweet"]
    print("=== RECENT ACTIVITY ON NEW ACCOUNTS / NEW CAMPAIGNS (last ~80 relevant lines) ===")
    relevant = []
    for l in lines[-300:]:
        if any(ph in l for ph in new_phones) or any(c in l for c in new_camps) or "cycle |" in l:
            relevant.append(l.rstrip())
    for l in relevant[-80:]:
        print(l)
else:
    print("no events log")
