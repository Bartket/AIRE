<#
.SYNOPSIS
    Build AIRE.exe on Windows.

.DESCRIPTION
    Creates a virtual environment, installs dependencies, regenerates the
    icon, and runs PyInstaller. The result is dist\AIRE\.

    Dependencies come from pyproject.toml, pinned by uv.lock so a build in
    six months produces the same thing as one today. uv is installed if it
    is missing; pass -NoUv to fall back to pip, which resolves fresh and is
    therefore not reproducible.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File build_windows.ps1
#>
param(
    [string]$Python = 'py',
    [string]$VenvPath = '.venv',
    [switch]$SkipInstall,
    [switch]$NoUv
)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Prefer uv: it resolves and installs in seconds rather than minutes, and
# uv.lock makes the build reproducible. Fall back to pip so a machine
# without it still builds.
$uv = $null
if (-not $NoUv) {
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCmd) { $uv = $uvCmd.Source }
    if (-not $uv -and -not $SkipInstall) {
        Write-Host '==> Installing uv' -ForegroundColor Cyan
        # pip writes upgrade notices to stderr, and with ErrorActionPreference
        # 'Stop' PowerShell turns those into terminating errors — which made a
        # successful install look like a failure and silently fell back to pip.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $Python -m pip install --quiet --upgrade uv 2>&1 | Out-Null
        $installed = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $prev

        if ($installed) {
            $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
            if ($uvCmd) {
                $uv = $uvCmd.Source
            } else {
                # pip puts it in the interpreter's Scripts directory, which is
                # not necessarily on PATH.
                $base = & $Python -c "import sysconfig; print(sysconfig.get_path('scripts'))"
                $candidate = Join-Path $base 'uv.exe'
                if (Test-Path $candidate) { $uv = $candidate }
            }
        }
        if (-not $uv) { Write-Warning 'Could not install uv; falling back to pip.' }
    }
}

$py = Join-Path $VenvPath 'Scripts\python.exe'

if ($uv) {
    if (-not $SkipInstall) {
        Write-Host '==> Installing dependencies with uv (locked)' -ForegroundColor Cyan
        # uv creates ".venv" by default; this honours -VenvPath instead.
        $env:UV_PROJECT_ENVIRONMENT = $VenvPath
        # --frozen uses uv.lock as-is rather than re-resolving, so the build
        # cannot quietly pick up a different version than was tested.
        # The release interpreter is pinned with the dependencies. Otherwise
        # a clean build months later could silently bundle a different Python.
        $BuildPython = (Get-Content '.python-version' -Raw).Trim()
        # uv reports progress on stderr, which ErrorActionPreference 'Stop'
        # would treat as a failure.
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & $uv sync --frozen --group build --python $BuildPython 2>&1 |
            ForEach-Object { Write-Host "    $_" }
        $ok = ($LASTEXITCODE -eq 0)
        $ErrorActionPreference = $prev
        if (-not $ok) { Write-Error 'uv sync failed' }
    }
    if (-not (Test-Path $py)) { Write-Error "uv did not create $py" }
} else {
    if (-not (Test-Path $VenvPath)) {
        Write-Host '==> Creating virtual environment' -ForegroundColor Cyan
        & $Python -m venv $VenvPath
    }
    if (-not $SkipInstall) {
        Write-Host '==> Installing dependencies with pip (not reproducible)' -ForegroundColor Yellow
        & $py -m pip install --upgrade pip
        & $py -m pip install -r requirements.txt
        & $py -m pip install pyinstaller
    }
}

if (-not (Test-Path 'ai_race_engineer\static\icon.ico')) {
    Write-Host '==> Generating icon' -ForegroundColor Cyan
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $py tools\make_icon.py 2>&1 | Out-Null
    $ErrorActionPreference = $prev
}

Write-Host '==> Building with PyInstaller' -ForegroundColor Cyan
# PyInstaller logs INFO to stderr, which ErrorActionPreference 'Stop' treats
# as a terminating error — this script could not run to completion before.
$prev = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& $py -m PyInstaller AIRE.spec --noconfirm --clean 2>&1 |
    Select-String -Pattern 'ERROR|PermissionError|Access is denied' |
    ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
$built = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev
if (-not $built) { Write-Error 'PyInstaller failed' }

$exe = 'dist\AIRE\AIRE.exe'
if (Test-Path $exe) {
    # Keep the legal terms and support material visible beside the executable.
    # PyInstaller otherwise hides data files under _internal in one-folder mode.
    Copy-Item 'LICENSE' 'dist\AIRE\LICENSE' -Force
    Copy-Item 'README.md' 'dist\AIRE\README.md' -Force
    Copy-Item 'CHANGELOG.md' 'dist\AIRE\CHANGELOG.md' -Force
    & $py tools\generate_third_party_notices.py --output THIRD_PARTY_NOTICES.txt
    if ($LASTEXITCODE -ne 0) { Write-Error 'Third-party notice generation failed' }
    Copy-Item 'THIRD_PARTY_NOTICES.txt' 'dist\AIRE\THIRD_PARTY_NOTICES.txt' -Force
    $size = [math]::Round((Get-ChildItem 'dist\AIRE' -Recurse |
                           Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    $version = (& $py -c "from ai_race_engineer import __version__; print(__version__)").Trim()
    if ($LASTEXITCODE -ne 0 -or -not $version) { Write-Error 'Could not read AIRE version' }
    $archive = "dist\AIRE-v$version-win64.zip"
    Compress-Archive -Path 'dist\AIRE' -DestinationPath $archive `
        -CompressionLevel Optimal -Force
    $digest = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    "$digest  $(Split-Path $archive -Leaf)" |
        Set-Content 'dist\SHA256SUMS.txt' -Encoding Ascii
    $builtAt = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss K')
    Write-Host "==> Built $exe ($size MB) at $builtAt" -ForegroundColor Green
    Write-Host "==> Release archive: $archive" -ForegroundColor Green
    Write-Host "==> SHA-256: $digest" -ForegroundColor Green
} else {
    Write-Error 'Build failed: executable not found'
}
