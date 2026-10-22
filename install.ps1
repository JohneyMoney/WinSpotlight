# Installerer WinSpotlight: Python (hvis det mangler), pywebview, selve appen og autostart.
$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$dst = Join-Path $env:USERPROFILE 'WinSpotlight'

function Step($t) { Write-Host "`n> $t" -ForegroundColor Cyan }

function Find-Python {
    $candidates = @()
    foreach ($c in 'py', 'python') {
        if (Get-Command $c -ErrorAction SilentlyContinue) { $candidates += $c }
    }
    foreach ($c in $candidates) {
        try {
            $exe = & $c -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
        } catch {}
    }
    $guess = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "C:\Python3*\python.exe" -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($guess) { return $guess.FullName }
    return $null
}

Write-Host "WinSpotlight - installasjon" -ForegroundColor White

Step "Ser etter Python"
$py = Find-Python
if (-not $py) {
    Write-Host "  Fant ikke Python. Installerer Python 3.13 med winget..."
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    $py = Find-Python
    if (-not $py) { throw "Klarte ikke å installere Python. Installer det fra python.org og kjør Installer.cmd på nytt." }
}
Write-Host "  Bruker $py"

Step "Installerer pywebview"
& $py -m pip install --user --upgrade --disable-pip-version-check --quiet pywebview
if ($LASTEXITCODE -ne 0) { throw "pip install pywebview feilet." }

Step "Stopper eventuell kjørende WinSpotlight"
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like '*spotlight.pyw*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Step "Kopierer filer til $dst"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item (Join-Path $src 'spotlight.pyw'), (Join-Path $src 'ui.html') $dst -Force
Get-ChildItem $dst -File | Unblock-File

Step "Slår på autostart (ved innlogging og når PC-en låses opp)"
& $py (Join-Path $dst 'spotlight.pyw') --autostart
if ($LASTEXITCODE -ne 0) { throw "Klarte ikke å sette opp autostart." }

Step "Starter WinSpotlight"
Start-ScheduledTask -TaskName WinSpotlight
Start-Sleep 6
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like '*spotlight.pyw*' }
if ($running) {
    Write-Host "`nFerdig! Trykk Ctrl+Mellomrom for å søke." -ForegroundColor Green
} else {
    Write-Host "`nInstallert, men appen startet ikke. Se $dst\data\feillogg.txt" -ForegroundColor Yellow
}
