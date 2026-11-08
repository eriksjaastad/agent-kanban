"""scripts/launchd/install-site-stats.sh (#8868): renders the site-stats LaunchAgent and loads it, repeatably."""

import os
import plistlib
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "launchd" / "install-site-stats.sh"
LABEL = "local.project-tracker.site-stats"


@pytest.fixture
def home(tmp_path):
    """A fake HOME with launchctl and trash stubs that log their calls; bootstrap fails when FAIL_BOOTSTRAP is set."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "launchctl").write_text(
        f'#!/bin/sh\necho "launchctl $1" >> {log}\n[ "$1" = bootstrap ] && [ -n "$FAIL_BOOTSTRAP" ] && exit 5\nexit 0\n')
    (bin_dir / "trash").write_text(f'#!/bin/sh\necho "trash" >> {log}\nexec /bin/rm "$@"\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("FAIL_BOOTSTRAP", None)
    return tmp_path, env, log


def run(env, action, **extra):
    return subprocess.run([str(SCRIPT), action], env={**env, **extra}, capture_output=True, text=True, timeout=30)


def test_install_renders_a_runnable_plist(home):
    tmp_path, env, _ = home
    assert run(env, "install").returncode == 0
    plist = plistlib.loads((tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist").read_bytes())
    args = plist["ProgramArguments"]
    assert args == [str(REPO / "scripts" / "record-run.sh"), LABEL, "--", str(REPO / "pt"), "site-stats", "run"]
    assert os.access(args[0], os.X_OK) and os.access(args[3], os.X_OK)
    path = plist["EnvironmentVariables"]["PATH"].split(":")
    assert "/opt/homebrew/bin" in path and f"{tmp_path}/.local/bin" in path  # doppler, uv
    assert plist["EnvironmentVariables"]["SEO_SPIDER_BIN"].endswith("/ScreamingFrogSEOSpiderLauncher")
    assert plist["StartCalendarInterval"] == {"Hour": 6, "Minute": 30} and "RunAtLoad" not in plist


def test_install_twice_reloads_and_uninstall_twice_is_safe(home):
    tmp_path, env, log = home
    dst = tmp_path / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    assert run(env, "install").returncode == 0 and run(env, "install").returncode == 0
    assert log.read_text().split("\n")[:4] == ["launchctl bootout", "launchctl bootstrap"] * 2
    first, second = run(env, "uninstall"), run(env, "uninstall")
    assert first.returncode == 0 and not dst.exists() and "removed" in first.stdout
    assert second.returncode == 0 and "skipped (not installed)" in second.stdout


def test_failed_bootstrap_fails_the_install(home):
    _, env, _ = home
    assert run(env, "install", FAIL_BOOTSTRAP="1").returncode != 0
