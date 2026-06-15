import os, zipfile, tempfile
zip_path = r'C:\Users\Administrator\Desktop\tdata.zip'
print('Zip exists:', os.path.exists(zip_path), 'size:', os.path.getsize(zip_path) if os.path.exists(zip_path) else 0)
with tempfile.TemporaryDirectory(prefix='tdata_inspect_') as tmp:
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(tmp)
    print('Top level after extract:')
    for name in sorted(os.listdir(tmp)):
        full = os.path.join(tmp, name)
        if os.path.isdir(full):
            print('  DIR:', name)
            for sub in os.listdir(full)[:8]:
                print('    ', sub)
        else:
            print('  FILE:', name)
