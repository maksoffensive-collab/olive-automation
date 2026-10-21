# Olive Automation (beta) -- installer for 64-bit Windows 10/11. Nothing else needs to be installed first, and no
# administrator rights are needed. One command, in PowerShell:
#
#   irm https://raw.githubusercontent.com/maksoffensive-collab/olive-automation/main/scripts/install.ps1 | iex
#
# It downloads the newest release from GitHub (betas included), checks the download against its SHA-256, puts the
# program in %LOCALAPPDATA%\Programs\Olive (replacing an older version there) and adds a Start menu and a desktop
# shortcut. Run it again to update. The person's macros, settings and history live in %LOCALAPPDATA%\Olive\data
# and are never touched. To remove the program: %LOCALAPPDATA%\Programs\Olive\Uninstall.ps1 (docs/RELEASE.md).
#
# For testing, environment variables override the defaults: OLIVE_REPO (owner/name on GitHub), OLIVE_ZIP (install
# this local zip instead of downloading; a .sha256 file next to it is checked when present), OLIVE_INSTALL_DIR,
# OLIVE_NO_SHORTCUTS=1.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'     # Windows PowerShell's progress bar slows downloads many times over
    $repo = if ($env:OLIVE_REPO) { $env:OLIVE_REPO } else { 'maksoffensive-collab/olive-automation' }
    $installDir = if ($env:OLIVE_INSTALL_DIR) { $env:OLIVE_INSTALL_DIR } else {
        Join-Path $env:LOCALAPPDATA 'Programs\Olive' }
    $installDir = [IO.Path]::GetFullPath($installDir).TrimEnd('\')
    $dataDir = Join-Path $env:LOCALAPPDATA 'Olive\data'

    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Olive Automation needs 64-bit Windows.' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
        [Net.SecurityProtocolType]::Tls12

    $parent = Split-Path $installDir -Parent
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    # Staged next to the destination, so the final move stays on one drive; a short name, as Windows limits a
    # path to 260 characters.
    $stage = Join-Path $parent ('.olive-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $stage | Out-Null
    $old = $null
    try {
        if ($env:OLIVE_ZIP) {
            $zip = [IO.Path]::GetFullPath($env:OLIVE_ZIP)
            $sumFile = "$zip.sha256"
            if (-not (Test-Path -LiteralPath $sumFile)) { $sumFile = $null }
            Write-Host "Installing from $zip"
        } else {
            Write-Host "Looking for the newest Olive Automation release on github.com/$repo ..."
            $headers = @{ 'User-Agent' = 'olive-installer'; 'Accept' = 'application/vnd.github+json' }
            $release = Invoke-RestMethod -UseBasicParsing -Headers $headers `
                -Uri "https://api.github.com/repos/$repo/releases?per_page=1" | Select-Object -First 1
            if (-not $release) { throw "github.com/$repo has no published release yet." }
            $asset = $release.assets | Where-Object { $_.name -like 'Olive-*-win64.zip' } | Select-Object -First 1
            $sumAsset = $release.assets | Where-Object { $_.name -eq "$($asset.name).sha256" } | Select-Object -First 1
            if (-not $asset -or -not $sumAsset) {
                throw "Release '$($release.name)' has no Olive-*-win64.zip with its .sha256 file."
            }
            $zip = Join-Path $stage $asset.name
            $sumFile = Join-Path $stage $sumAsset.name
            $mb = [Math]::Round($asset.size / 1MB)
            Write-Host "Downloading $($asset.name) ($mb MB) ..."
            Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $asset.browser_download_url -OutFile $zip
            Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $sumAsset.browser_download_url -OutFile $sumFile
        }
        if ($sumFile) {
            $expected = ((Get-Content -LiteralPath $sumFile -Raw).Trim() -split '\s+')[0]
            $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
            if ($actual -ne $expected) {
                throw "The download is damaged (SHA-256 $actual, expected $expected). Nothing was changed."
            }
            Write-Host 'SHA-256 checked.'
        }

        Expand-Archive -LiteralPath $zip -DestinationPath $stage
        $staged = Join-Path $stage 'Olive'
        if (-not (Test-Path -LiteralPath (Join-Path $staged 'Olive.exe'))) {
            throw 'The archive does not hold Olive\Olive.exe. Nothing was changed.'
        }
        Get-ChildItem -LiteralPath $staged -Recurse -File | Unblock-File

        if (Test-Path -LiteralPath $installDir) {
            $isOlive = Test-Path -LiteralPath (Join-Path $installDir 'Olive.exe')
            $isEmpty = -not (Get-ChildItem -LiteralPath $installDir -Force | Select-Object -First 1)
            if (-not ($isOlive -or $isEmpty)) {
                throw "$installDir exists and is not an Olive installation. Nothing was changed."
            }
            $running = Get-Process -Name 'Olive' -ErrorAction SilentlyContinue | Where-Object {
                $_.Path -and $_.Path.StartsWith($installDir + '\', [StringComparison]::OrdinalIgnoreCase) }
            if ($running) { throw 'Olive Automation is running. Close it, then run the installer again.' }
            $old = Join-Path $stage 'previous'
            Move-Item -LiteralPath $installDir -Destination $old
        }
        try {
            Move-Item -LiteralPath $staged -Destination $installDir
        } catch {
            if ($old) { Move-Item -LiteralPath $old -Destination $installDir }
            throw
        }

        $exe = Join-Path $installDir 'Olive.exe'
        if ($env:OLIVE_NO_SHORTCUTS -ne '1') {
            $shell = New-Object -ComObject WScript.Shell
            foreach ($place in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('Desktop'))) {
                $link = $shell.CreateShortcut((Join-Path $place 'Olive Automation (beta).lnk'))
                $link.TargetPath = $exe
                $link.WorkingDirectory = $installDir
                $link.Description = 'Olive Automation (beta)'
                $link.Save()
            }
        }
        $version = (Get-Content -LiteralPath (Join-Path $installDir 'VERSION.txt') -TotalCount 1)
        Write-Host ''
        Write-Host "Installed: $version" -ForegroundColor Green
        Write-Host "  Program:  $installDir"
        Write-Host "  Your data (macros, settings, history): $dataDir"
        if ($env:OLIVE_NO_SHORTCUTS -ne '1') {
            Write-Host "  Start it from the Start menu or the desktop: 'Olive Automation (beta)'."
        }
        Write-Host '  The first start may show "Windows protected your PC": this beta is not signed.'
        Write-Host '  Choose "More info", then "Run anyway".'
    } finally {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    }
}
