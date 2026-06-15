@echo off
setlocal
cd /d "%~dp0"
echo Creating TELETON data backup...
python safe_backup_data.py
if errorlevel 1 (
  echo.
  echo Backup failed. Do not close Teleton until data is checked.
) else (
  echo.
  echo Backup complete.
)
echo.
pause
