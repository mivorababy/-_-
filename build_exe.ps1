$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location -LiteralPath $root

try {
    # --- Python: the preferred install, then the py launcher, then python.exe from PATH
    $python = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        $python = $null
        foreach ($name in @("py.exe", "python.exe")) {
            $found = Get-Command $name -ErrorAction SilentlyContinue
            if ($found) { $python = $found.Source; break }
        }
    }
    if (-not $python) { throw "Python was not found. Install Python from python.org and enable Add to PATH." }
    Write-Host "Python: $python"
    & $python --version
    if ($LASTEXITCODE -ne 0) { throw "Python could not start." }

    # --- a running copy of the freshly built EXE would block overwriting it
    $targetExe = Join-Path $root "dist\NOT soundLARP 100%.exe"
    Get-CimInstance Win32_Process -Filter "Name = 'NOT soundLARP 100%.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -eq $targetExe } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    Write-Host "Installing Python packages and PyInstaller..."
    & $python -m pip install --upgrade -r requirements.txt pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip failed. Check the output above." }
    Write-Host "Verifying Discord presence and online search dependencies..."
    & $python -c "import pypresence, yt_dlp; print('Discord Rich Presence and yt-dlp imports OK')"
    if ($LASTEXITCODE -ne 0) { throw "Discord Rich Presence or online search dependencies could not be imported by the selected Python." }

    Write-Host "Preparing FFmpeg and ffprobe..."
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "prepare_ffmpeg.ps1")
    if ($LASTEXITCODE -ne 0) { throw "FFmpeg preparation failed." }
    foreach ($file in @("ffmpeg\bin\ffmpeg.exe", "ffmpeg\bin\ffprobe.exe", "web\index.html", "icon.ico", "LastFrequency.spec", "windows-version-info.txt")) {
        if (-not (Test-Path -LiteralPath (Join-Path $root $file))) { throw "Required file missing: $file" }
    }

    # --- the whole build is described by the .spec file (one onefile EXE, web UI and FFmpeg are inside it)
    Write-Host "Building NOT soundLARP 100%.exe..."
    & $python -m PyInstaller --noconfirm --clean LastFrequency.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed. Check the output above." }
    if (-not (Test-Path -LiteralPath $targetExe)) { throw "PyInstaller returned without producing the EXE." }

    $size = [math]::Round((Get-Item -LiteralPath $targetExe).Length / 1MB)
    Write-Host "Build complete: dist\NOT soundLARP 100%.exe ($size MB)"
    Write-Host "The EXE includes the web UI, FFmpeg and ffprobe. Run build_installer.bat to make the installer."
    exit 0
} catch {
    Write-Host "BUILD FAILED: $_" -ForegroundColor Red
    exit 1
}
