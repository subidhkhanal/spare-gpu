# spare-gpu installer for Windows. Paste this one line into PowerShell:
#
#   irm https://raw.githubusercontent.com/subidhkhanal/spare-gpu/master/install.ps1 | iex
#
# It installs Python and git with winget if they're missing, installs spare-gpu into its own
# Python environment (%LOCALAPPDATA%\spare-gpu), connects it to BharatCode Desktop with every chat
# command (`gtu integrate --pack`) and puts `gtu` on your PATH. Run it again to update. Nothing needs admin rights
# except, possibly, the git install (Windows may ask).
#
# Options, as environment variables set before running it:
#   SPARE_GPU_HOME     where to install (default %LOCALAPPDATA%\spare-gpu)
#   SPARE_GPU_VERSION  which release to install, e.g. 0.1.0 (default: the newest release on GitHub)
#   SPARE_GPU_SOURCE   what to install instead, anything pip accepts (overrides SPARE_GPU_VERSION)
#   SPARE_GPU_REPO     the GitHub repository to install from, owner/name (default subidhkhanal/spare-gpu; a fork sets its own)
#   SPARE_GPU_SKIP_INTEGRATE=1  install only; don't touch BharatCode's config
#   SPARE_GPU_SKIP_PATH=1       don't add gtu to your PATH

function Resolve-SpareGpuSource($repo) {
    # A known version, not whatever master holds at this minute: the newest tagged release.
    if ($env:SPARE_GPU_SOURCE) { return $env:SPARE_GPU_SOURCE }
    if ($env:SPARE_GPU_VERSION) { return "https://github.com/$repo/archive/refs/tags/v$($env:SPARE_GPU_VERSION).zip" }
    $tag = $null
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -Headers @{ 'User-Agent' = 'spare-gpu-installer' }
        $tag = $release.tag_name
    } catch { $tag = $null }
    if ($tag) {
        Write-Host "spare-gpu: installing release $tag"
        return "https://github.com/$repo/archive/refs/tags/$tag.zip"
    }
    Write-Host 'spare-gpu: no release is published yet, so this installs the development version from master.' -ForegroundColor Yellow
    return "https://github.com/$repo/archive/refs/heads/master.zip"
}

function Install-SpareGpu {
    $ErrorActionPreference = 'Stop'
    $home_ = if ($env:SPARE_GPU_HOME) { $env:SPARE_GPU_HOME } else { Join-Path $env:LOCALAPPDATA 'spare-gpu' }
    $repo = if ($env:SPARE_GPU_REPO) { $env:SPARE_GPU_REPO } else { 'subidhkhanal/spare-gpu' }
    $source = Resolve-SpareGpuSource $repo

    function Say($text) { Write-Host "spare-gpu: $text" }
    function Fail($text) { Write-Host "spare-gpu: $text" -ForegroundColor Red; throw 'spare-gpu install stopped' }

    function Update-PathFromRegistry {
        # winget installs update PATH in the registry; this window still has the old one.
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path', 'User')
    }

    function Find-Python {
        # A Python 3.11+ that really runs. The Microsoft Store's "python" placeholder doesn't.
        $candidates = @(@{ exe = 'py'; extra = @('-3') }, @{ exe = 'python'; extra = @() }, @{ exe = 'python3'; extra = @() })
        foreach ($candidate in $candidates) {
            $exe = $candidate.exe
            if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
            $extra = $candidate.extra
            try {
                $out = & $exe @extra -c "import sys; print(sys.version_info >= (3, 11), sys.executable)" 2>$null
            } catch { continue }
            if ($LASTEXITCODE -eq 0 -and $out -match '^True (.+)$') { return $Matches[1].Trim() }
        }
        return $null
    }

    function Install-WithWinget($id, $what) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Fail "$what isn't installed, and winget (Windows' package manager) isn't available to install it. Install $what yourself, then run this again."
        }
        Say "installing $what with winget..."
        & winget install --id $id --exact --silent --accept-source-agreements --accept-package-agreements | Out-Null
        if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {  # that code: already installed
            Fail "winget couldn't install $what (exit code $LASTEXITCODE). Install it yourself, then run this again."
        }
        Update-PathFromRegistry
    }

    # 1. Python 3.11 or newer
    $python = Find-Python
    if (-not $python) {
        Install-WithWinget 'Python.Python.3.12' 'Python 3.12'
        $python = Find-Python
        if (-not $python) {
            $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
            if (Test-Path $guess) { $python = $guess } else { Fail 'Python was installed but this window cannot see it yet. Open a new PowerShell window and run this again.' }
        }
    }
    Say "using Python at $python"

    # 2. git: spare-gpu uses it to keep each job's private copy (your project doesn't need git)
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Install-WithWinget 'Git.Git' 'git'
        if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
            Fail 'git was installed but this window cannot see it yet. Open a new PowerShell window and run this again.'
        }
    }

    # 3. spare-gpu in its own environment, so it can't clash with anything else you've installed
    $venv = Join-Path $home_ 'venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        Say "creating its environment in $venv"
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { Fail "couldn't create a Python environment in $venv." }
    }
    Say 'installing spare-gpu...'
    & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade $source
    if ($LASTEXITCODE -ne 0) { Fail 'pip could not install spare-gpu (see above).' }
    # the same version number can carry new code: always take the latest files
    & $venvPython -m pip install --quiet --disable-pip-version-check --force-reinstall --no-deps $source
    if ($LASTEXITCODE -ne 0) { Fail 'pip could not install spare-gpu (see above).' }
    $gtu = Join-Path $venv 'Scripts\gtu.exe'
    $installed = (& $gtu --version 2>$null | Select-Object -Last 1)
    if ($LASTEXITCODE -ne 0 -or -not $installed) { Fail 'spare-gpu was installed but `gtu --version` did not run (see above).' }
    Say "installed spare-gpu $installed"

    # 4. `gtu` on your PATH, for new windows (`gtu integrate --uninstall`)
    $scripts = Split-Path $gtu
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }  # a fresh account may have no user PATH at all
    if ($env:SPARE_GPU_SKIP_PATH -ne '1' -and -not (($userPath -split ';') -contains $scripts)) {
        [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ';' + $scripts).TrimStart(';'), 'User')
        Say "added $scripts to your PATH (new windows will have the gtu command)"
    }
    $env:Path = "$scripts;$env:Path"

    # 5. connect it to BharatCode Desktop: the spare-gpu tools and every chat command
    if ($env:SPARE_GPU_SKIP_INTEGRATE -eq '1') {
        Say 'installed. Skipped connecting to BharatCode Desktop (SPARE_GPU_SKIP_INTEGRATE=1).'
        return
    }
    & $gtu integrate --pack
    if ($LASTEXITCODE -ne 0) { Fail 'gtu integrate --pack reported a problem (see above).' }

    Write-Host ''
    Write-Host "spare-gpu $installed is installed." -ForegroundColor Green
    Write-Host 'Now fully close BharatCode Desktop and open it again. Then, in any chat, try for example:'
    Write-Host '  /code-review          /auto write docs for src/utils.py          /doctor'
    Write-Host 'To remove it later: gtu integrate --uninstall, then delete' $home_
}

# SPARE_GPU_NO_RUN=1 loads the functions without installing (the tests parse the script that way).
if ($env:SPARE_GPU_NO_RUN -ne '1') {
    try { Install-SpareGpu } catch { if ($_.Exception.Message -ne 'spare-gpu install stopped') { Write-Host "spare-gpu: $($_.Exception.Message)" -ForegroundColor Red } }
}
