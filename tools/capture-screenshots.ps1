<#
.SYNOPSIS
    Recapture the README screenshots.

.DESCRIPTION
    Starts AIRE on a throwaway config and port, drives Edge headless over
    each settings tab, and writes docs\images\*.png.

    The throwaway config is the point. Useful screenshots need a populated
    UI, but a populated UI is somebody's actual setup: API keys, wheel
    bindings, driver name, voice library. Capturing against a scratch
    config means nothing personal can reach the repo by accident, and the
    result is what a new user sees on first run rather than what the
    author's machine happens to look like.

    A separate port means this never disturbs a running instance - you can
    recapture mid-session without closing the app you are racing with.

    Tabs are addressed by URL fragment (#voice, #engineer, ...), which is
    what makes this scriptable at all; without that a headless browser only
    ever sees the tab the app opens on.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\capture-screenshots.ps1

.EXAMPLE
    # Against your own settings, for a richer voice list. Check every image
    # for personal data before committing it.
    powershell -ExecutionPolicy Bypass -File tools\capture-screenshots.ps1 -UseMyConfig
#>
param(
    [int]$Port = 9431,
    [int]$Width = 1180,
    [int]$Height = 900,
    [switch]$UseMyConfig,
    [string]$OutDir = "$PSScriptRoot\..\docs\images"
)

# ASCII only in this file. PowerShell 5.1 reads .ps1 as ANSI unless the file
# has a UTF-8 BOM, so a single em-dash silently breaks string parsing several
# lines later and the errors point nowhere near the cause.
#
# Native tools write progress to stderr; Stop would abort on the first line.
$ErrorActionPreference = 'Continue'

$edge = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $edge) { Write-Host 'Microsoft Edge not found - cannot capture.'; exit 1 }

$exe = Join-Path $PSScriptRoot '..\dist\AIRE\AIRE.exe'
if (-not (Test-Path $exe)) { Write-Host "No build at $exe - run the build first."; exit 1 }

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

# Scratch config, unless explicitly told to use the real one.
$scratch = Join-Path $env:TEMP ("aire-shots-" + [guid]::NewGuid().ToString('N') + '.json')
$appArgs = @('--server', '--no-browser', '--no-tray', '--port', $Port)
if (-not $UseMyConfig) {
    [System.IO.File]::WriteAllText($scratch, '{}')  # no BOM: PS 5.1's -Encoding UTF8 adds one
    $appArgs += @('--config', $scratch)
    Write-Host "Using a scratch config: $scratch"
} else {
    Write-Host 'Using YOUR config - check every image for personal data before committing.'
}

Write-Host "Starting AIRE on port $Port..."
# Redirect the child's streams explicitly. AIRE logs to stderr, and when this
# script's own output is redirected to a file the inherited handles can leave
# the child blocked on a write that nobody drains - it then never finishes
# starting and the wait below times out with no clue why.
$appOut = Join-Path $env:TEMP ("aire-shot-out-" + [guid]::NewGuid().ToString('N') + '.log')
$appErr = Join-Path $env:TEMP ("aire-shot-err-" + [guid]::NewGuid().ToString('N') + '.log')
$app = Start-Process $exe -ArgumentList $appArgs -PassThru `
       -RedirectStandardOutput $appOut -RedirectStandardError $appErr
try {
    # Wait for the server rather than sleeping a guessed amount.
    $ready = $false
    foreach ($i in 1..120) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod "http://127.0.0.1:$Port/api/status" -TimeoutSec 3 | Out-Null
            $ready = $true; break
        } catch { }
    }
    if (-not $ready) {
        Write-Host 'AIRE did not answer in time.'
        Write-Host 'First launch after a rebuild is slow while Windows scans the new binary.'
        Write-Host 'Run it once by hand, then try again.'
        exit 1
    }
    Write-Host ("Ready after " + $i + "s.")

    $tabs = @('race', 'engineer', 'voice', 'audio', 'input', 'app')
    foreach ($tab in $tabs) {
        $out = Join-Path $OutDir "$tab.png"
        $profile = Join-Path $env:TEMP ("edge-shot-" + [guid]::NewGuid().ToString('N'))
        & $edge --headless=new --disable-gpu --hide-scrollbars `
                --user-data-dir="$profile" `
                --window-size="$Width,$Height" `
                --screenshot="$out" `
                --virtual-time-budget=6000 `
                "http://127.0.0.1:$Port/#$tab" 2>$null | Out-Null
        Remove-Item -Recurse -Force $profile -ErrorAction SilentlyContinue

        if (Test-Path $out) {
            $kb = [math]::Round((Get-Item $out).Length / 1KB)
            Write-Host ("  " + $tab.PadRight(10) + $kb + " KB")
        } else {
            Write-Host ("  " + $tab.PadRight(10) + "FAILED")
        }
    }
} finally {
    if ($app -and -not $app.HasExited) { $app | Stop-Process -Force }
    Remove-Item $scratch -ErrorAction SilentlyContinue
    Remove-Item $appOut, $appErr -ErrorAction SilentlyContinue
}

Write-Host "Done. Images in $OutDir"
Write-Host 'Look at each one before committing - a screenshot is published forever.'
