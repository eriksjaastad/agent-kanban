"""pt site-stats: run the website stats collector for every site (#8868).

Each sites/<name>.json is a seo_monitor config plus a "doppler" list naming
the Doppler projects, configs and secrets that site's collector needs. `run`
runs `python -m seo_monitor --config <file> run` under `doppler run` for each
site, one at a time, and stores a site_stats row: the collector's exit code
and, when that run wrote a fresh status.json, each source's status, flags,
period and metrics.

seo_monitor exits 2 when a source is only stale or partial; that is a normal
run. `run` exits 1 only when some site's collector exited with anything else.
"""

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from db.manager import DatabaseManager

ROOT = Path(__file__).resolve().parents[2]
SITES = ROOT / "sites"
TIMEOUT = 3600  # a crawl, a sitemap crawl and PageSpeed runs, each bounded inside seo_monitor
# Screaming Frog's launcher on this Mac. seo_monitor runs `screamingfrogseospider` from
# PATH unless SEO_SPIDER_BIN names the executable, and the app ships no PATH entry.
SPIDER = "/Applications/Screaming Frog SEO Spider.app/Contents/MacOS/ScreamingFrogSEOSpiderLauncher"


def _name(value) -> bool:
    return isinstance(value, str) and value != "" and "\0" not in value


def command(path: Path, cfg: dict) -> list[str]:
    """The collector's argv; ValueError when the doppler list is not usable as argv.

    "doppler" is a list of {project, config, secrets} blocks, one `doppler run`
    each, nested, so a site can take its Google credential from one project and
    its Bing key from another. A secret name may appear in only one block: an
    inner block would otherwise silently replace an outer one's value.
    """
    blocks, argv, seen = cfg["doppler"], [], set()
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("doppler must be a non-empty list of {project, config, secrets}")
    for block in blocks:
        secrets = block["secrets"]
        if not (_name(block["project"]) and _name(block["config"])
                and isinstance(secrets, list) and secrets and all(map(_name, secrets))):
            raise ValueError("each doppler block needs project and config names and a non-empty list of secret names")
        if seen & set(secrets):
            raise ValueError(f"secret named in two doppler blocks: {sorted(seen & set(secrets))}")
        seen |= set(secrets)
        argv += ["doppler", "run", "--project", block["project"], "--config", block["config"],
                 "--only-secrets", ",".join(secrets), "--"]
    return argv + [sys.executable, "-m", "seo_monitor", "--config", str(path), "run"]


def _run(argv: list[str]) -> int:
    """Run argv in its own process group; returns its exit code, or 124 on timeout.

    However this returns (done, timed out, interrupted), the whole group is
    killed: killing only doppler would leave seo_monitor running with its lock
    held, so the next run would fail as "already running".
    """
    env = dict(os.environ)
    if not env.get("SEO_SPIDER_BIN") and Path(SPIDER).exists():
        env["SEO_SPIDER_BIN"] = SPIDER  # so a bare `pt site-stats run` crawls like the scheduled one
    proc = subprocess.Popen(argv, cwd=ROOT, start_new_session=True, env=env)
    try:
        return proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return 124
    finally:
        with contextlib.suppress(ProcessLookupError):  # the group already exited
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def summary(status: dict) -> dict:
    """Each source's status, flags, period and metrics from a status.json.

    A source with no reading yet ("missing") or an unreadable one ("error")
    has only a status and an error: no flags, no observation.
    """
    out = {}
    for name, entry in status["sources"].items():
        obs = entry.get("observation") or {}
        out[name] = {"status": entry["status"], "flags": entry.get("flags", []), "error": entry.get("error"),
                     "captured_at": obs.get("captured_at"), "period_start": obs.get("period_start"),
                     "period_end": obs.get("period_end"), "metrics": obs.get("metrics")}
    return out


def failures(status: dict) -> list[str]:
    """Why a run failed, from its status.json: each source error and failed crawl attempt."""
    out = [f"{name}: {entry['error']}" for name, entry in status["sources"].items()
           if entry.get("status") == "error" and entry.get("error")]
    crawl = status.get("last_crawl_attempt") or {}
    coverage = (status.get("sitemap_coverage") or {}).get("last_attempt") or {}
    for label, attempt in (("crawl", crawl), ("sitemap coverage", coverage)):
        if attempt.get("error"):  # only a failed attempt carries one
            out.append(f"{label}: {attempt['error']}")
    return out


def run_site(path: Path, started: datetime, run=_run) -> tuple[int, str | None]:
    """Run one site's collector; returns its exit code and this run's sources as JSON.

    A failed collector still writes a status with its last good readings, so
    that is kept too. A config that can't be read, or a 0/2 exit without a
    readable fresh status, counts as exit 1: a broken site never passes as normal.
    """
    try:
        cfg = json.loads(path.read_text())
        command_line = command(path, cfg)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        click.echo(f"{path.stem}: bad site config: {exc}", err=True)
        return 1, None
    try:
        code = run(command_line)
    except OSError as exc:  # doppler or python missing: nothing ran
        click.echo(f"{path.stem}: could not start the collector: {exc}", err=True)
        return 127, None
    try:
        status = json.loads(((path.parent / cfg["state_dir"]).resolve() / "status.json").read_text())
        if datetime.fromisoformat(status["checked_at"]) < started:
            raise ValueError(f"status.json is from {status['checked_at']}, before this run")
        if code not in (0, 2):
            for reason in failures(status):
                click.echo(f"{path.stem}: {reason}", err=True)
        return code, json.dumps(summary(status))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        click.echo(f"{path.stem}: no fresh status: {exc}", err=True)
        return (1 if code in (0, 2) else code), None


@click.group(name="site-stats")
def site_stats_group() -> None:
    """Website stats from the central collector (sites/*.json)."""


@site_stats_group.command(name="run")
def site_stats_run() -> None:
    """Collect every site's stats and store them; exits 1 if a collector failed."""
    db, failed = DatabaseManager(), []
    for path in sorted(SITES.glob("*.json")):
        started = datetime.now(timezone.utc)
        try:
            code, sources = run_site(path, started)
        # governance: allow-silent SF002: the error is printed, the site is stored as exit 1 and fails the run
        except Exception as exc:  # noqa: BLE001 - one site's unexpected error must not stop the next site
            click.echo(f"{path.stem}: collector run failed: {exc!r}", err=True)
            code, sources = 1, None
        click.echo(f"{path.stem}: exit {code}")
        try:
            db.site_stats_recorded(path.stem, started.isoformat(timespec="seconds"), code, sources)
        except (sqlite3.Error, OSError) as exc:  # e.g. migration 026 not applied yet
            click.echo(f"{path.stem}: could not store the run: {exc}", err=True)
            code = 1
        if code not in (0, 2):
            failed.append(path.stem)
    if failed:
        raise click.ClickException("collector failed for " + ", ".join(failed))
