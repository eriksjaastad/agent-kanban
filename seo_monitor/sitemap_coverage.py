"""Crawl coverage measured against the site's own sitemap; standard library only.

The sitemap is the coverage denominator only when each configured API-derived
section matches its source count, read at the same time. A sitemap that shrank
during an outage would otherwise make coverage look better. Labels:

- ``complete``: every check matched and the list crawl finished under its limit;
- ``capped``: the sitemap or the list crawl reached the crawler's URL limit;
- ``degraded``: a completeness check failed or mismatched, or the crawl's
  completion could not be confirmed. Never reported as complete.

Configured with an optional ``sitemap_coverage`` object; absent means off.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from .crawl_diff import crawl_coverage, html_pages, load_export

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
HTTP_TIMEOUT_SECONDS = 30
MAX_COUNT_PAGES = 1000
MAX_CHILD_SITEMAPS = 50
DEFAULT_LIST_LIMIT = 50


def http_get(url, timeout=HTTP_TIMEOUT_SECONDS):
    """Return the body of an HTTP 200 response; anything else raises."""
    request = urllib.request.Request(url, headers={"User-Agent": "seo-monitor"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status} from {url}")
        return response.read()


def validate_config(spec, site):
    """Reject a configuration that could not produce an honest denominator."""
    if not isinstance(spec, dict):
        raise TypeError("sitemap_coverage must be an object")
    sitemap = urlparse(spec.get("sitemap", ""))
    if sitemap.scheme not in {"http", "https"}:
        raise ValueError("sitemap_coverage.sitemap must be an HTTP(S) URL")
    if sitemap.hostname != urlparse(site).hostname:
        raise ValueError("sitemap_coverage.sitemap must be on the configured site")
    if int(spec.get("list_limit", DEFAULT_LIST_LIMIT)) <= 0:
        raise ValueError("sitemap_coverage.list_limit must be positive")
    params = spec.get("pagination_params", ["page"])
    if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
        raise ValueError("sitemap_coverage.pagination_params must be strings")
    for check in spec.get("completeness_checks", []):
        if not str(check.get("path_prefix", "")).startswith("/"):
            raise ValueError("completeness check path_prefix must start with /")
        if urlparse(check.get("url", "")).scheme not in {"http", "https"}:
            raise ValueError("completeness check url must be HTTP(S)")
        count = check.get("count")
        if not isinstance(count, dict) or ("field" in count) == ("list" in count):
            raise ValueError("completeness check count needs exactly one of field/list")


def normalize(url):
    """Compare URLs without scheme/host case or fragments; keep path and query."""
    parts = urlsplit(url.strip())
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def sitemap_urls(url, get=None):
    """All <loc> URLs, following one level of sitemap index. Duplicates kept once."""
    get = get or http_get
    root = ET.fromstring(get(url))
    if root.tag == SITEMAP_NS + "sitemapindex":
        children = [
            loc.text.strip() for loc in root.iter(SITEMAP_NS + "loc") if loc.text
        ]
        if len(children) > MAX_CHILD_SITEMAPS:
            raise ValueError(f"sitemap index lists {len(children)} sitemaps")
        urls = []
        for child in children:
            child_root = ET.fromstring(get(child))
            if child_root.tag != SITEMAP_NS + "urlset":
                raise ValueError(f"nested sitemap index at {child}")
            urls += [
                loc.text for loc in child_root.iter(SITEMAP_NS + "loc") if loc.text
            ]
    elif root.tag == SITEMAP_NS + "urlset":
        urls = [loc.text for loc in root.iter(SITEMAP_NS + "loc") if loc.text]
    else:
        raise ValueError(f"{url} is not a sitemap (root element {root.tag})")
    return list(dict.fromkeys(u.strip() for u in urls))


def source_count(check, get=None):
    """Count from a numeric JSON field, or by walking a cursor-paginated list."""
    get = get or http_get
    spec = check["count"]
    if "field" in spec:
        value = json.loads(get(check["url"]))[spec["field"]]
        if type(value) is not int or value < 0:
            raise ValueError(f"{spec['field']} is not a nonnegative integer")
        return value
    cursor_field = spec.get("cursor", "next_cursor")
    cursor_param = spec.get("cursor_param", "cursor")
    url, total = check["url"], 0
    for _ in range(MAX_COUNT_PAGES):
        body = json.loads(get(url))
        items = body[spec["list"]]
        if not isinstance(items, list):
            raise TypeError(f"{spec['list']} is not a list")
        total += len(items)
        cursor = body.get(cursor_field)
        if cursor is None:
            return total
        parts = urlsplit(check["url"])
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != cursor_param]
        url = urlunsplit(
            parts._replace(query=urlencode(query + [(cursor_param, cursor)]))
        )
    raise ValueError(f"more than {MAX_COUNT_PAGES} pages")


def completeness(checks, urls, get=None):
    """Each API-derived section must equal its source count; failure is not zero."""
    results = []
    for check in checks:
        prefix = check["path_prefix"]
        listed = sum(1 for u in urls if urlsplit(u).path.startswith(prefix))
        entry = {"path_prefix": prefix, "source": check["url"], "sitemap_count": listed}
        try:
            expected = source_count(check, get)
            entry.update(source_count=expected, ok=expected == listed)
        except Exception as exc:  # noqa: BLE001 - any failure degrades the check
            entry.update(source_count=None, ok=False, error=type(exc).__name__)
        results.append(entry)
    return results


def is_pagination(url, params):
    """True for a query made only of numeric pagination parameters, e.g. ?page=2."""
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    return bool(query) and all(k in params and v.isdigit() for k, v in query)


def outside_sitemap(rows, listed, site, params):
    """Indexable HTML pages on the site that the sitemap does not list."""
    host = urlparse(site).hostname
    return sorted(
        url
        for url, row in html_pages(rows).items()
        if urlparse(url).hostname == host
        and row.get("Indexability") == "Indexable"
        and str(row.get("Status Code", "")) == "200"
        and normalize(url) not in listed
        and not is_pagination(url, params)
    )


def interval(cfg):
    spec = cfg["sitemap_coverage"]
    return timedelta(days=spec.get("interval_days", cfg["crawl"]["interval_days"]))


def config_fingerprint(cfg):
    """What a reading measured: site, sitemap URL, checks and limits.

    Excludes the crawler executable, which a machine-local override may change
    between the scheduled run and an operator's `status`.
    """
    scope = {
        "site": cfg["site"],
        "coverage": cfg["sitemap_coverage"],
        "limit": cfg["crawl"]["limit"],
    }
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def collect(cfg, state, clock, get=None, *, secret_env):
    """Run when due; failed attempts keep the last good reading and its evidence.

    ``secret_env`` names the reporting secrets the crawler must never receive;
    the caller passes the same tuple it strips from its own crawl.
    """
    spec = cfg.get("sitemap_coverage")
    if not spec:
        return
    crawl = dict(cfg["crawl"])
    if os.environ.get("SEO_SPIDER_BIN"):
        crawl["command"] = [os.environ["SEO_SPIDER_BIN"]]
    fingerprint = hashlib.sha256(
        json.dumps(
            {"site": cfg["site"], "coverage": spec, **crawl}, sort_keys=True
        ).encode()
    ).hexdigest()
    current_path = state / "sitemap_coverage.json"
    attempt_path = state / "sitemap-coverage-attempt.json"
    previous = read(current_path) if current_path.exists() else None
    last_attempt = read(attempt_path) if attempt_path.exists() else None
    # Only a complete reading waits for the interval. A degraded or capped one
    # is retried on the next run, so a briefly unavailable count API recovers.
    if (
        previous
        and previous.get("fingerprint") == fingerprint
        and previous.get("config_fingerprint") == config_fingerprint(cfg)
        and previous.get("label") == "complete"
        and (not last_attempt or last_attempt["status"] == "success")
        and clock - datetime.fromisoformat(previous["captured_at"]) < interval(cfg)
    ):
        return
    attempt = {"attempted_at": clock.isoformat(), "status": "running"}
    write(attempt_path, attempt)
    try:
        write(
            current_path,
            measure(
                cfg, spec, crawl, state, clock, fingerprint, previous, get, secret_env
            ),
        )
        attempt["status"] = "success"
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ET.ParseError,
        subprocess.SubprocessError,
    ) as exc:
        attempt.update(status="error", error=f"{type(exc).__name__}: {exc}")
    write(attempt_path, attempt)


def measure(cfg, spec, crawl, state, clock, fingerprint, previous, get, secret_env):
    limit = int(spec.get("list_limit", DEFAULT_LIST_LIMIT))
    params = spec.get("pagination_params", ["page"])
    urls = sitemap_urls(spec["sitemap"], get)
    if not urls:
        raise ValueError("sitemap lists no URLs")
    checks = completeness(spec.get("completeness_checks", []), urls, get)
    run = (
        state
        / "sitemap-crawls"
        / (clock.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    )
    run.mkdir(parents=True)
    write(run / "sitemap-urls.json", urls)
    with (run / "crawl.log").open("w") as log:
        subprocess.run(
            crawl["command"]
            + [
                "--crawl-sitemap",
                spec["sitemap"],
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
            env={k: v for k, v in os.environ.items() if k not in secret_env},
            check=True,
        )
    rows = {
        normalize(url): row
        for url, row in load_export(run / "internal_all.csv").items()
    }
    crawl_state, crawled = crawl_coverage(
        (run / "crawl.log").read_text(), crawl["limit"]
    )
    if len(urls) >= crawl["limit"]:
        crawl_state = "capped"

    fetched = ok = non_indexable = 0
    missing, unchecked = [], []
    for url in urls:
        row = rows.get(normalize(url))
        code = str(row.get("Status Code", "")).strip() if row else ""
        if row and code not in {"", "0"}:
            fetched += 1
            if row.get("Indexability") != "Indexable":
                non_indexable += 1
        if row and code == "200" and row.get("Indexability") == "Indexable":
            ok += 1
            continue
        if not row or code in {"", "0"}:
            # Only a crawl confirmed complete can show a URL was never fetched;
            # a capped or unconfirmed crawl may simply not have reached it.
            if crawl_state != "complete":
                unchecked.append(url)
                continue
            reason = "not fetched"
        else:
            reason = f"HTTP {code}; {row.get('Indexability') or 'no indexability'}"
            if row.get("Indexability Status"):
                reason += f" ({row['Indexability Status']})"
        missing.append({"url": url, "reason": reason})

    listed = {normalize(u) for u in urls}
    homepage, outside, homepage_export = None, [], None
    crawl_obs = state / "screaming_frog.json"
    if crawl_obs.exists():
        homepage = read(crawl_obs)
        homepage_export = homepage["export_path"]
        outside = outside_sitemap(
            load_export(state / homepage_export), listed, cfg["site"], params
        )

    denominator_ok = all(c["ok"] for c in checks)
    if not denominator_ok or crawl_state == "unknown":
        label = "degraded"
    elif crawl_state == "capped":
        label = "capped"
    else:
        label = "complete"

    limitations = []
    for check in checks:
        if not check["ok"]:
            limitations.append(
                f"Denominator degraded: {check['sitemap_count']} sitemap URLs under "
                f"{check['path_prefix']} vs source count "
                + (
                    f"{check['source_count']}."
                    if check["source_count"] is not None
                    else f"unavailable ({check['error']})."
                )
            )
    if crawl_state == "capped":
        limitations.append(
            f"List crawl capped: sitemap_urls={len(urls)}, crawled={crawled}, "
            f"limit={crawl['limit']}. {len(unchecked)} sitemap URLs were not reached; "
            "they are unchecked, not failures."
        )
    if crawl_state == "unknown":
        limitations.append(
            "List crawl completion was not confirmed in the crawl log; "
            f"{len(unchecked)} sitemap URLs were not reached; they are unchecked, not failures."
        )
    if homepage is None:
        limitations.append(
            "No homepage crawl yet; indexable pages outside the sitemap are unknown."
        )
    else:
        limitations.append(
            "indexable_not_in_sitemap uses the homepage crawl captured "
            f"{homepage['captured_at']} and excludes numeric pagination "
            f"({', '.join(f'?{p}=N' for p in params) or 'none configured'})."
        )

    findings = [
        f"Sitemap URL not OK: {m['url']} ({m['reason']})" for m in missing[:limit]
    ]
    if len(missing) > limit:
        findings.append(f"...and {len(missing) - limit} more sitemap URLs not OK.")
    findings += [f"Indexable page outside the sitemap: {u}" for u in outside[:limit]]
    if len(outside) > limit:
        findings.append(
            f"...and {len(outside) - limit} more indexable pages outside the sitemap."
        )
    if (
        previous
        and previous.get("label") == "complete"
        and len(urls) < previous["metrics"]["sitemap_urls"]
    ):
        findings.append(
            f"Sitemap shrank from {previous['metrics']['sitemap_urls']} to {len(urls)} "
            "URLs since the last complete snapshot; confirm the unpublishing was intended."
        )

    return {
        "version": 1,
        "site": cfg["site"],
        "sitemap": spec["sitemap"],
        "captured_at": clock.isoformat(),
        "label": label,
        "crawl_state": crawl_state,
        "metrics": {
            "sitemap_urls": len(urls),
            "sitemap_fetched": fetched,
            "sitemap_ok": ok,
            "sitemap_not_ok": len(missing),
            "sitemap_unchecked": len(unchecked),
            "non_indexable_in_sitemap": non_indexable,
            "indexable_not_in_sitemap": len(outside),
            "crawled_urls": crawled,
        },
        "sitemap_missing": missing[:limit],
        "sitemap_unchecked": unchecked[:limit],
        "indexable_not_in_sitemap": outside[:limit],
        "completeness_checks": checks,
        "limitations": limitations,
        "findings": findings,
        "export_path": str((run / "internal_all.csv").relative_to(state)),
        "homepage_export_path": homepage_export,
        "fingerprint": fingerprint,
        "config_fingerprint": config_fingerprint(cfg),
    }


def status(cfg, state, clock):
    """Report entry plus its exit contribution (0 current, 2 attention, 1 error)."""
    if not cfg.get("sitemap_coverage"):
        return None, 0
    current_path = state / "sitemap_coverage.json"
    attempt_path = state / "sitemap-coverage-attempt.json"
    entry = {"sitemap": cfg["sitemap_coverage"]["sitemap"]}
    attempt = read(attempt_path) if attempt_path.exists() else None
    if attempt:
        entry["last_attempt"] = attempt
    if current_path.exists():
        reading = read(current_path)
        entry["reading"] = reading
        due = datetime.fromisoformat(reading["captured_at"]) + interval(cfg)
        changed = reading.get("config_fingerprint") != config_fingerprint(cfg)
        if changed:
            entry["note"] = "Configuration changed since this reading."
        entry["due_at"] = due.isoformat()
        entry["flags"] = [
            flag
            for flag, raised in (
                ("stale", clock >= due or changed),
                (reading["label"], reading["label"] != "complete"),
                ("findings", bool(reading["findings"])),
            )
            if raised
        ]
        entry["status"] = "attention" if entry["flags"] else "current"
    else:
        entry["status"] = "missing"
    if attempt and attempt["status"] != "success":
        entry.update(
            status="error",
            error="Sitemap coverage failed: " + attempt.get("error", "interrupted run"),
        )
    code = {"current": 0, "error": 1}.get(entry["status"], 2)
    return entry, code


def report_lines(entry):
    lines = [
        "",
        f"## sitemap_coverage: {entry['status']}",
        f"Sitemap: {entry['sitemap']}",
    ]
    reading = entry.get("reading")
    if reading:
        lines += [
            f"Captured: {reading['captured_at']}",
            f"Coverage: {reading['label']} (list crawl {reading['crawl_state']})",
            f"Next run due: {entry['due_at']}",
            "Flags: " + ", ".join(entry["flags"]),
            "Metrics: " + json.dumps(reading["metrics"], sort_keys=True),
        ]
        if entry.get("note"):
            lines.append(entry["note"])
        for check in reading["completeness_checks"]:
            lines.append(
                f"Check {check['path_prefix']}: sitemap {check['sitemap_count']}, "
                f"source {check['source_count']}, {'ok' if check['ok'] else 'FAILED'}"
            )
        lines += [f"- {x}" for x in reading["limitations"] + reading["findings"]]
    if entry.get("error"):
        lines.append("Collection error: " + entry["error"])
    if not reading and not entry.get("error"):
        lines.append("No sitemap coverage reading yet.")
    return lines


def read(path: Path):
    return json.loads(path.read_text())


def write(path: Path, value):
    """Atomic replace, as in __main__.write; imported there, so not imported back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        handle.write(json.dumps(value, indent=2) + "\n")
    os.replace(handle.name, path)
