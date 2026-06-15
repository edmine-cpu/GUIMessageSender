import os
bad = ["+17246484305", "+12368913226", "+447988550363"]
removed = 0
for b in bad:
    p = os.path.join("data/sessions", f"session_{b}.session")
    for ext in ("", "-journal", "-wal", "-shm"):
        pp = p + ext
        if os.path.exists(pp):
            try:
                os.remove(pp)
                removed += 1
            except:
                pass
print("Cleaned", removed, "old session files for the 3 banned accounts (optional cleanup)")
