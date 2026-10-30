# One-time Windows setup for lcxl3_fx.py:
#   - creates .venv next to this script and installs mido + python-rtmidi
#   - registers the "LCXL3-FX" scheduled task (starts at logon, no console window,
#     restarts itself if it ever crashes)
#   - adds a "LED FX Control" Start Menu shortcut to the control panel
#
# Needs Python 3.12 (python-rtmidi ships Windows wheels for it):
#   winget install -e --id Python.Python.3.12 --scope user
#
# Run:  powershell -ExecutionPolicy Bypass -File .\install_windows.ps1

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$taskName = 'LCXL3-FX'

# --- Python environment
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $python)) {
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
    if (-not $python) { throw 'Python 3.12 not found. Install it with: winget install -e --id Python.Python.3.12 --scope user' }
    $pyArgs = @('-3.12')
} else { $pyArgs = @() }

$venv = Join-Path $root '.venv'
if (-not (Test-Path $venv)) { & $python @pyArgs -m venv $venv }
& (Join-Path $venv 'Scripts\python.exe') -m pip install --upgrade pip
& (Join-Path $venv 'Scripts\python.exe') -m pip install -r (Join-Path $root 'requirements.txt')

# --- Autostart task
$pythonw = Join-Path $venv 'Scripts\pythonw.exe'
$script = Join-Path $root 'lcxl3_fx.py'
$logFile = Join-Path $root 'lcxl3_fx.log'
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`" --log `"$logFile`"" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = 'PT15S'   # give USB/MIDI drivers a moment after logon
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'Ambient LED effects for the Launch Control XL 3' | Out-Null

# --- Start Menu shortcut to the control panel
$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'LED FX Control.url'
Set-Content -Path $shortcut -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8765/" -Encoding ascii

Start-ScheduledTask -TaskName $taskName
Write-Host "Installed. Control panel: http://127.0.0.1:8765  (log: $logFile)"
