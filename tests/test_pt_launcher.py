"""The `pt` launcher runs without Doppler.

Nothing in `pt` needs a Doppler secret, and `pt launch` needs none at all. The launcher used to wrap everything in `doppler run` unless
~/projects/.turso-config.json said otherwise, so a machine without that file
paid for (and depended on) Doppler on every call.
"""
import os
import shutil
import stat
import threading
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "pt"
REAL_UV = shutil.which("uv") or os.path.expanduser("~/.local/bin/uv")


def _sandbox(tmp_path: Path) -> dict[str, str]:
    """An environment with no ~/projects/.turso-config.json and a doppler that fails."""
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # The launcher runs the uv on PATH: this one hands over to the real uv with the real HOME.
    shim = fake_bin / "uv"
    shim.write_text(f'#!/bin/sh\nHOME="{Path.home()}" exec "{REAL_UV}" "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    called = tmp_path / "doppler-called"
    doppler = fake_bin / "doppler"
    doppler.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 99\n')
    doppler.chmod(doppler.stat().st_mode | stat.S_IXUSR)

    env = dict(os.environ)
    env.pop("PT_SKIP_DOPPLER", None)
    env.update(
        HOME=str(home),
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        PT_DB_PATH=str(tmp_path / "tracker.db"),
        PT_SUPPRESS_MIGRATION_WARNING="1",
    )
    return env


@pytest.mark.parametrize("skip_doppler", [None, "1"], ids=["bare", "inherited_skip_doppler"])
def test_bare_pt_never_invokes_doppler(tmp_path, skip_doppler):
    env = _sandbox(tmp_path)
    if skip_doppler is not None:
        # The retired PT_SKIP_DOPPLER variable is ignored: a harmless no-op.
        env["PT_SKIP_DOPPLER"] = skip_doppler
    # --version, not --help: the old launcher skipped Doppler for --help only.
    result = subprocess.run([str(LAUNCHER), "--version"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "doppler-called").exists()


def test_pt_launch_runs_uvicorn_without_doppler(monkeypatch):
    """The dashboard needs no secrets: `pt launch` runs uvicorn itself, even with no doppler installed."""
    import pt
    from click.testing import CliRunner

    calls, threads = [], []

    class NoThread:
        """Stands in for the browser-opening thread, so nothing outlives the test."""
        def __init__(self, target, daemon):
            threads.append(target)

        def start(self):
            pass

    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.delenv("PT_DASHBOARD_HOST", raising=False)
    monkeypatch.setattr(pt, "init_db", lambda: None)
    monkeypatch.setattr(threading, "Thread", NoThread)
    monkeypatch.setattr(pt.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    result = CliRunner().invoke(pt.cli, ["launch", "--no-scan", "--port", "8123"])
    assert result.exit_code == 0, result.output
    assert len(threads) == 1  # the browser thread was built and never started
    assert len(calls) == 1 and calls[0][1:] == ["-m", "uvicorn", "dashboard.app:app", "--host", "127.0.0.1",
                                                 "--port", "8123"]


def test_without_uv_on_path_the_launcher_uses_uvs_default_location(tmp_path):
    env = _sandbox(tmp_path)
    (tmp_path / "bin" / "uv").rename(tmp_path / "home" / ".local-uv")
    default = Path(env["HOME"]) / ".local" / "bin"
    default.mkdir(parents=True)
    (tmp_path / "home" / ".local-uv").rename(default / "uv")
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin"
    result = subprocess.run([str(LAUNCHER), "--version"], env=env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
