# Olive Automation (beta) -- removes the program from this computer: the folder this script is in and the
# 'Olive Automation (beta)' shortcuts. Copied into the release as Uninstall.ps1 (scripts/build_release.py). Run:
#
#   powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\Olive\Uninstall.ps1"
#
# The person's macros, settings and history in %LOCALAPPDATA%\Olive\data are kept; delete that folder by hand to
# remove them too.

$here = $PSScriptRoot
& {
    $ErrorActionPreference = 'Stop'
    if (-not $here -or -not (Test-Path -LiteralPath (Join-Path $here 'Olive.exe'))) {
        throw 'Run this file from the Olive Automation program folder (next to Olive.exe). Nothing was removed.'
    }
    $running = Get-Process -Name 'Olive' -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -and $_.Path.StartsWith($here + '\', [StringComparison]::OrdinalIgnoreCase) }
    if ($running) { throw 'Olive Automation is running. Close it, then run this again. Nothing was removed.' }
    foreach ($place in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
        $link = Join-Path $place 'Olive Automation (beta).lnk'
        if (Test-Path -LiteralPath $link) { Remove-Item -LiteralPath $link -Force }
    }
    Set-Location -LiteralPath ([IO.Path]::GetTempPath())
    Remove-Item -LiteralPath $here -Recurse -Force
    Write-Host 'Olive Automation was removed.' -ForegroundColor Green
    Write-Host "Your data was kept: $(Join-Path $env:LOCALAPPDATA 'Olive\data')"
}
