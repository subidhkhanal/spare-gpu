"""install.ps1 installs a tagged release, not master, and says which version it installed.
PowerShell itself runs only on Windows (the project's CI); elsewhere the script's text is checked."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_the_default_source_is_the_newest_tagged_release_not_master():
    assert "api.github.com/repos/$repo/releases/latest" in TEXT and "archive/refs/tags/$tag.zip" in TEXT
    master = [m.start() for m in re.finditer(r"refs/heads/master\.zip", TEXT)]
    assert len(master) == 1  # master is only the fallback while no release exists...
    assert TEXT.index("no release is published yet") < master[0]  # ...and is announced before it is used


def test_a_version_or_a_source_can_be_pinned():
    assert "refs/tags/v$($env:SPARE_GPU_VERSION).zip" in TEXT
    assert "if ($env:SPARE_GPU_SOURCE) { return $env:SPARE_GPU_SOURCE }" in TEXT
    assert "$repo = if ($env:SPARE_GPU_REPO) { $env:SPARE_GPU_REPO } else { 'subidhkhanal/spare-gpu' }" in TEXT
    for option in ("SPARE_GPU_VERSION", "SPARE_GPU_SOURCE", "SPARE_GPU_REPO"):
        assert re.search(rf"^#\s+{option}\s", TEXT, re.M), f"{option} is documented at the top"


def test_the_installed_version_is_printed_and_checked():
    assert "& $gtu --version" in TEXT and 'Say "installed spare-gpu $installed"' in TEXT
    assert "did not run" in TEXT  # a broken install is reported, not announced as done


def test_a_missing_user_path_does_not_abort_the_installer():
    assert "if (-not $userPath) { $userPath = '' }" in TEXT  # a fresh account may have no user PATH at all


def test_the_script_loads_without_installing_when_asked():
    assert "if ($env:SPARE_GPU_NO_RUN -ne '1')" in TEXT


def powershell():
    return shutil.which("powershell") or shutil.which("pwsh")


@pytest.mark.skipif(sys.platform != "win32" or not powershell(), reason="PowerShell runs the installer on Windows")
def test_powershell_parses_the_script_and_resolves_a_pinned_version():
    env = {k: v for k, v in os.environ.items() if k != "SPARE_GPU_SOURCE"}
    env.update(SPARE_GPU_NO_RUN="1", SPARE_GPU_VERSION="1.2.3")
    out = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                          f". '{SCRIPT}'; Resolve-SpareGpuSource 'subidhkhanal/spare-gpu'"],
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "https://github.com/subidhkhanal/spare-gpu/archive/refs/tags/v1.2.3.zip"
    env["SPARE_GPU_SOURCE"] = "C:\\dev\\spare-gpu"  # a source wins over a version, and nothing is fetched
    out = subprocess.run([powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                          f". '{SCRIPT}'; Resolve-SpareGpuSource 'subidhkhanal/spare-gpu'"],
                         capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0 and out.stdout.strip().splitlines()[-1] == "C:\\dev\\spare-gpu"
