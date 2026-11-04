$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$script = Join-Path $root "LastFrequency.iss"
$candidates = @()
$command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($command) { $candidates += $command.Source }
foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($base) { $candidates += (Join-Path $base "Inno Setup 6\ISCC.exe") }
}
if ($env:LOCALAPPDATA) {
    $candidates += (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
}
$iscc = $candidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install Inno Setup, then run this script again." }
$payload = Join-Path $root "dist\NOT soundLARP 100%.exe"
if (-not (Test-Path -LiteralPath $payload)) { throw "Build the player first: dist\NOT soundLARP 100%.exe is missing." }
& $iscc $script
if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed with exit code $LASTEXITCODE." }
$setup = Join-Path $root "dist\NOT soundLARP 100%-Setup.exe"
# Existing installed versions recognize this setup filename when checking updates.
Copy-Item -LiteralPath $setup -Destination (Join-Path $root 'dist\LastFrequency-Setup.exe') -Force
$size = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB)
Write-Host "Installer ready: $setup ($size MB)"
