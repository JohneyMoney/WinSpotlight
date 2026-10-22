@echo off
chcp 65001 >nul
echo Avinstallerer WinSpotlight...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Unregister-ScheduledTask -TaskName WinSpotlight -Confirm:$false -ErrorAction SilentlyContinue;" ^
  "Remove-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name WinSpotlight -ErrorAction SilentlyContinue;" ^
  "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*spotlight.pyw*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force };" ^
  "Start-Sleep 1; Remove-Item -Recurse -Force \"$env:USERPROFILE\WinSpotlight\" -ErrorAction SilentlyContinue"
echo Ferdig.
pause
