import os, zipfile, tempfile
zip_path = r'C:\Users\Administrator\Desktop\tdata.zip'
with tempfile.TemporaryDirectory(prefix='tdata_inspect2_') as tmp:
    with zipfile.ZipFile(zip_path) as z: z.extractall(tmp)
    # Find candidate tdata roots
    candidates = []
    for root, dirs, files in os.walk(tmp):
        if 'key_datas' in files or any(f.startswith('D877F783') for f in files):
            candidates.append(root)
    print('Found tdata content dirs (pass these to TDesktop):')
    for c in candidates:
        print('  ', c)
