$ErrorActionPreference = "SilentlyContinue"

Get-CimInstance Win32_Process -Filter "name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*TELETON_NEW_RUN*" -or $_.CommandLine -like "*gui.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Start-Sleep -Seconds 2

$taskName = "TeletonStartOnce"
$pythonw = "C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe"
$workdir = "C:\Users\Administrator\Desktop\TELETON_NEW_RUN"
$bat = Join-Path $workdir "start_teleton_once.bat"

Set-Content -Path $bat -Encoding ASCII -Value @"
@echo off
cd /d "$workdir"
"$pythonw" gui.py
"@

$startTime = (Get-Date).AddMinutes(1).ToString("HH:mm")
schtasks /Create /TN $taskName /SC ONCE /ST $startTime /TR "`"$bat`"" /RL HIGHEST /IT /F | Out-Null
schtasks /Run /TN $taskName | Out-Null
Start-Sleep -Seconds 4
schtasks /Delete /TN $taskName /F | Out-Null

Get-CimInstance Win32_Process -Filter "name='pythonw.exe'" |
    Select-Object ProcessId, CommandLine
