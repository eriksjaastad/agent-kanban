"""Portable SEO collection/import/freshness reports; standard library only.

Owns Screaming Frog/optional Google API collection and console imports, not account
login, browser scraping, site changes or publishing. Configuration is trusted.
Exit 0: current scoped data; 2: attention/partial/stale; 1: input/collection error.
State/evidence are private local files. Supports macOS/Linux process locking.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import pagespeed, sitemap_coverage
from .crawl_diff import crawl_coverage, html_pages, load_export
from .crawl_diff import report as crawl_report

SOURCES = ("gsc", "ga4", "ahrefs", "screaming_frog")
# Optional sources may be omitted from a site configuration (#7805).
OPTIONAL_SOURCES = ("bing", "pagespeed")
# Reporting secrets are never passed to the crawler subprocess.
SECRET_ENV = ("SEO_GOOGLE_CREDENTIALS", "BING_WEBMASTER_API_KEY")


def now():
    return datetime.now(timezone.utc)  # noqa: UP017 - project compatibility contract


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("timestamps require a timezone")
    return result


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Preserve a failed temporary write for diagnosis; never replace last-good data.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        handle.write(
            value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
        )
    os.replace(handle.name, path)


def config(path):
    cfg = read(path)
    if urlparse(cfg["site"]).scheme not in {"http", "https"}:
        raise ValueError("site must be an HTTP(S) URL")
    if not set(SOURCES) <= set(cfg["sources"]) <= set(SOURCES + OPTIONAL_SOURCES):
        raise ValueError("configure all four sources, including unavailable ones")
    for spec in cfg["sources"].values():
        if (
            not spec["property"]
            or not 0 < spec["fresh_days"] <= 90
            or spec["max_data_age_days"] <= 0
        ):
            raise ValueError("property and positive freshness windows required")
    if cfg["sources"]["screaming_frog"]["property"] != cfg["site"]:
        raise ValueError("crawler property must match site")
    crawl = cfg["crawl"]
    if not (
        isinstance(crawl["command"], list)
        and crawl["command"]
        and all(isinstance(x, str) for x in crawl["command"])
    ):
        raise ValueError("crawl command must be an argv list")
    if min(crawl["interval_days"], crawl["limit"], crawl["timeout_seconds"]) <= 0:
        raise ValueError("crawl intervals, limits and timeouts must be positive")
    if "pagespeed" in cfg["sources"]:
        pagespeed.validate_config(cfg["sources"]["pagespeed"], cfg["site"])
    if cfg.get("sitemap_coverage"):
        sitemap_coverage.validate_config(cfg["sitemap_coverage"], cfg["site"])
    return cfg, (path.parent / cfg["state_dir"]).resolve()


def validate(obs, cfg, clock):
    if not isinstance(obs, dict):
        raise TypeError("observation must be an object")
    source = obs["source"]
    if obs["version"] != 1 or source not in cfg["sources"]:
        raise ValueError("unsupported observation version/source")
    if obs["property"] != cfg["sources"][source]["property"]:
        raise ValueError("observation belongs to a different property")
    captured = timestamp(obs["captured_at"])
    start, end = (
        date.fromisoformat(obs["period_start"]),
        date.fromisoformat(obs["period_end"]),
    )
    if captured > clock + timedelta(minutes=5) or not start <= end <= captured.date():
        raise ValueError("inconsistent or future observation dates")
    if obs["coverage"] not in {"scoped", "partial"} or not obs["scope"].strip():
        raise ValueError("scope and coverage required")
    for field in ("limitations", "findings"):
        if not isinstance(obs[field], list) or not all(
            isinstance(x, str) for x in obs[field]
        ):
            raise ValueError(f"{field} must be strings")
    if obs["coverage"] == "partial" and not obs["limitations"]:
        raise ValueError("partial coverage needs a stated limitation")
    if not isinstance(obs["metrics"], dict) or not obs["metrics"]:
        raise ValueError("metrics required; use null for unavailable readings")
    for value in obs["metrics"].values():
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
        ):
            raise ValueError("metrics must be nonnegative finite numbers or null")
    return obs


def save_observation(obs, cfg, state, evidence, clock):
    validate(obs, cfg, clock)
    current_path = state / (obs["source"] + ".json")
    if current_path.exists() and timestamp(
        read(current_path)["captured_at"]
    ) > timestamp(obs["captured_at"]):
        raise ValueError("older capture cannot replace the current observation")
    raw = evidence.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    archived = state / "evidence" / digest
    if not archived.exists():
        archived.parent.mkdir(parents=True, exist_ok=True)
        with archived.open("xb") as handle:
            handle.write(raw)
        archived.chmod(0o600)
    obs = {
        **obs,
        "evidence_sha256": digest,
        "evidence_path": str(archived.relative_to(state)),
    }
    write(state / "history" / obs["source"] / (uuid.uuid4().hex + ".json"), obs)
    write(state / (obs["source"] + ".json"), obs)


def collect(cfg, state, clock):
    """Failed attempts cannot advance successful capture time or erase its evidence."""
    crawl = dict(cfg["crawl"])
    if os.environ.get("SEO_SPIDER_BIN"):
        crawl["command"] = [os.environ["SEO_SPIDER_BIN"]]
    fingerprint = hashlib.sha256(
        json.dumps({"site": cfg["site"], **crawl}, sort_keys=True).encode()
    ).hexdigest()
    previous_path = state / "screaming_frog.json"
    previous = read(previous_path) if previous_path.exists() else None
    if previous:
        validate(previous, cfg, clock)
        if previous.get("crawl_fingerprint") == fingerprint and clock - timestamp(
            previous["captured_at"]
        ) < timedelta(days=crawl["interval_days"]):
            return
    run = (
        state
        / "crawls"
        / (clock.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    )
    run.mkdir(parents=True)
    attempt = {"attempted_at": clock.isoformat(), "status": "running"}
    write(state / "crawl-attempt.json", attempt)
    try:
        with (run / "crawl.log").open("w") as log:
            subprocess.run(
                crawl["command"]
                + [
                    "--crawl",
                    cfg["site"],
                    "--headless",
                    "--output-folder",
                    str(run),
                    "--export-format",
                    "csv",
                    "--export-tabs",
                    "Internal:All",
                ],
                stdout=log,
                stderr=log,
                timeout=crawl["timeout_seconds"],
                env={k: v for k, v in os.environ.items() if k not in SECRET_ENV},
                check=True,
            )
        csv_path = run / "internal_all.csv"
        rows = load_export(csv_path)
        if not rows or not any(
            urlparse(url).hostname == urlparse(cfg["site"]).hostname for url in rows
        ):
            raise ValueError("empty export or configured site absent from export")
        coverage, fetched = crawl_coverage(
            (run / "crawl.log").read_text(), crawl["limit"]
        )
        findings = []
        pages = html_pages(rows)
        broken = sum(
            1
            for r in pages.values()
            if str(r.get("Status Code", "")).startswith(("4", "5"))
        )
        missing = sum(
            1
            for r in pages.values()
            if r.get("Indexability") == "Indexable" and not r.get("Title 1", "").strip()
        )
        if broken:
            findings.append(f"Inspect {broken} HTML responses with HTTP 4xx/5xx.")
        if missing:
            findings.append(f"Inspect {missing} indexable HTML pages without titles.")
        if previous and previous.get("crawl_fingerprint") != fingerprint:
            findings.append(
                "Crawl configuration changed: establish a new comparison baseline."
            )
        if previous and previous.get("crawl_fingerprint") == fingerprint:
            old = state / previous["export_path"]
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                result = crawl_report(old, csv_path, complete=coverage == "complete")
            write(run / "diff.txt", text.getvalue())
            if result == 1:
                findings.append(
                    f"Investigate crawl changes: {(run / 'diff.txt').relative_to(state)}"
                )
        obs = {
            "version": 1,
            "source": "screaming_frog",
            "property": cfg["site"],
            "captured_at": clock.isoformat(),
            "period_start": clock.date().isoformat(),
            "period_end": clock.date().isoformat(),
            "scope": "Default HTML crawl; JS off; configured URL limit",
            "coverage": "scoped" if coverage == "complete" else "partial",
            "limitations": []
            if coverage == "complete"
            else [
                f"Crawl {coverage}; fetched={fetched}, limit={crawl['limit']}. Unseen URLs are not proven removed."
            ],
            "metrics": {
                "exported_urls": len(rows),
                "html_pages": len(pages),
                "fetched_urls": fetched,
                "http_errors": broken,
                "missing_titles": missing,
            },
            "findings": findings,
            "export_path": str(csv_path.relative_to(state)),
            "crawl_fingerprint": fingerprint,
        }
        save_observation(obs, cfg, state, csv_path, clock)
        attempt["status"] = "success"
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        attempt.update(status="error", error=f"{type(exc).__name__}: {exc}")
    write(state / "crawl-attempt.json", attempt)


def collect_google(cfg, state, clock):
    """Independent sources retain last-good data and visible failed-attempt state."""
    from . import google

    for source in ("gsc", "ga4"):
        if not cfg["sources"][source].get("google_api", False):
            continue
        attempt_path = state / (source + "-attempt.json")
        attempt = {"attempted_at": clock.isoformat(), "status": "running"}
        client = None
        try:
            path = state / (source + ".json")
            if path.exists():
                previous = validate(read(path), cfg, clock)
                if previous.get("collector") == google.COLLECTOR and google.report_day(
                    clock
                ) == google.report_day(timestamp(previous["captured_at"])):
                    continue
            write(attempt_path, attempt)
            client = google.session()
            observation, evidence = google.collect(
                source, cfg["sources"][source]["property"], clock, client.post
            )
            evidence_path = (
                state / "google" / (source + "-" + uuid.uuid4().hex + ".json")
            )
            write(evidence_path, evidence)
            save_observation(observation, cfg, state, evidence_path, clock)
            attempt["status"] = "success"
        except Exception as exc:  # noqa: BLE001 - independent collector boundary
            # Collector boundary: record failure, continue the other independent source.
            # SDK/transport exceptions can contain sensitive details; retain only type.
            attempt.update(
                status="error",
                error=str(exc)
                if isinstance(exc, google.GoogleReportError)
                else type(exc).__name__,
            )
        finally:
            if client is not None:
                client.close()
        write(attempt_path, attempt)


def status(cfg, state, clock):
    sources = {}
    for source in [s for s in SOURCES + OPTIONAL_SOURCES if s in cfg["sources"]]:
        path = state / (source + ".json")
        spec = cfg["sources"][source]
        entry = {
            "access": spec["access"],
            "property": spec["property"],
            "dashboard": spec["dashboard"],
        }
        try:
            obs = validate(read(path), cfg, clock)
            if obs["source"] != source:
                raise ValueError("source filename mismatch")
            if (
                hashlib.sha256((state / obs["evidence_path"]).read_bytes()).hexdigest()
                != obs["evidence_sha256"]
            ):
                raise ValueError("evidence hash mismatch")
            due = timestamp(obs["captured_at"]) + timedelta(days=spec["fresh_days"])
            old_period = clock.date() - date.fromisoformat(
                obs["period_end"]
            ) > timedelta(days=spec["max_data_age_days"])
            flags = []
            if clock >= due or old_period:
                flags.append("stale")
            if obs["coverage"] == "partial":
                flags.append("partial")
            if obs.get("findings"):
                flags.append("findings")
            if any(v is None for v in obs["metrics"].values()):
                flags.append("unavailable_metrics")
            entry.update(
                status="attention" if flags else "current",
                flags=flags,
                due_at=due.isoformat(),
                observation=obs,
            )
        except FileNotFoundError as exc:
            entry.update(
                status="missing" if not path.exists() else "error", error=str(exc)
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            entry.update(status="error", error=str(exc))
        api_attempt = state / (source + "-attempt.json")
        api = any(spec.get(k, False) for k in ("google_api", "bing_api", "pagespeed_api"))
        if api and api_attempt.exists():
            attempt = read(api_attempt)
            entry["last_api_attempt"] = attempt
            if attempt["status"] != "success":
                entry.update(
                    status="error",
                    error={"bing": "Bing", "pagespeed": "PageSpeed"}.get(source, "Google")
                    + " collection failed: "
                    + attempt.get("error", "interrupted run"),
                )
        sources[source] = entry
    attempt_path = state / "crawl-attempt.json"
    attempt = read(attempt_path) if attempt_path.exists() else None
    failed = attempt and attempt["status"] != "success"
    code = (
        1
        if failed or any(s["status"] == "error" for s in sources.values())
        else (2 if any(s["status"] != "current" for s in sources.values()) else 0)
    )
    coverage, coverage_code = sitemap_coverage.status(cfg, state, clock)
    if code != 1 and coverage_code:
        code = coverage_code
    result = {
        "site": cfg["site"],
        "checked_at": clock.isoformat(),
        "exit_code": code,
        "sources": sources,
        "last_crawl_attempt": attempt,
    }
    if coverage:
        result["sitemap_coverage"] = coverage
    lines = [
        f"# SEO monitoring: {cfg['site']}",
        f"Checked: {clock.isoformat()}",
        "",
        "Enabled Google APIs refresh daily; browser-only sources require an agent refresh.",
    ]
    for source, entry in sources.items():
        lines += [
            "",
            f"## {source}: {entry['status']}",
            f"Property: {entry['property']}",
            f"Access: {entry['access']}",
            f"Dashboard: {entry['dashboard']}",
        ]
        obs = entry.get("observation")
        if obs:
            lines += [
                f"Captured: {obs['captured_at']}; period: {obs['period_start']} through {obs['period_end']}",
                f"Scope: {obs['scope']}",
                f"Next refresh due: {entry['due_at']}",
                "Metrics: " + json.dumps(obs["metrics"], sort_keys=True),
                "Flags: " + ", ".join(entry["flags"]),
            ]
            lines += [f"- {x}" for x in obs["limitations"] + obs.get("findings", [])]
            if obs.get("comparison"):
                comparison = obs["comparison"]
                lines += [
                    f"Previous equal window: {comparison['period_start']} through {comparison['period_end']}",
                    "Previous metrics: "
                    + json.dumps(comparison["metrics"], sort_keys=True),
                    "Absolute changes (current minus previous): "
                    + json.dumps(comparison["changes"], sort_keys=True),
                ]
            if obs.get("rankings"):
                lines += [
                    "Query/page examples (up to10 of each kind; full returned rows in source JSON):"
                ]
                counts = {}
                for row in obs["rankings"]:
                    kind = row.get("kind", "example")
                    counts[kind] = counts.get(kind, 0) + 1
                    if counts[kind] <= 10:
                        lines.append("    " + json.dumps(row, ensure_ascii=True))
            if obs.get("pages"):
                lines += pagespeed.report_lines(obs)
            if entry.get("error"):
                lines.append("Collection error: " + entry["error"])
        else:
            lines.append(
                "Collect or repair this source: " + entry.get("error", "missing")
            )
    if coverage:
        lines += sitemap_coverage.report_lines(coverage)
    if failed:
        lines += ["", "Crawl attempt failed or was interrupted: " + json.dumps(attempt)]
    write(state / "status.json", result)
    write(state / "report.md", "\n".join(lines) + "\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("run", help="crawl when due and refresh monitoring status")
    commands.add_parser("status", help="recompute freshness without network access")
    record = commands.add_parser(
        "record", help="import an agent-verified console reading"
    )
    record.add_argument("observation", type=Path)
    record.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        cfg, state = config(args.config.resolve())
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        import fcntl

        with (state / "monitor.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("SEO monitor already running", file=sys.stderr)
                return 1
            clock = now()
            if args.action == "record":
                observation = read(args.observation)
                if observation["source"] == "screaming_frog":
                    raise ValueError("use run for crawler collection")
                save_observation(observation, cfg, state, args.evidence, clock)
            elif args.action == "run":
                collect_google(cfg, state, clock)
                from . import bing

                bing.collect_source(cfg, state, clock, sys.modules[__name__])
                pagespeed.collect_source(cfg, state, clock, sys.modules[__name__])
                collect(cfg, state, clock)
                sitemap_coverage.collect(cfg, state, clock, secret_env=SECRET_ENV)
            result = status(cfg, state, clock)
            if args.action == "run":
                # The scheduled job's log: one line per run. status.json and
                # report.md hold the detail (#8262).
                print(f"{result['checked_at']} exit {result['exit_code']}: " + ", ".join(
                    f"{source}={entry['status']}" for source, entry in result["sources"].items()))
            else:
                print(json.dumps(result, indent=2))
            return 0 if args.action == "record" else result["exit_code"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"SEO monitor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
