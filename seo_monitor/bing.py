"""Read-only Bing Webmaster API adapter (standard library only).

Methods (Bing Webmaster API, JSON endpoint, API-key access):
GetUserSites, GetCrawlStats, GetQueryStats, GetRankAndTrafficStats and
GetUrlSubmissionQuota. https://learn.microsoft.com/en-us/bingwebmaster/getting-access

The API key travels in the query string, so every URL kept in evidence or an error
is built without it. An empty list is Bing saying "no data yet", recorded as
unavailable (null), never as a measured zero. Bounded: one request per method,
30s timeout, no retries; HTTP 429 is recorded as rate-limited.
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

COLLECTOR = "bing-webmaster-api-v1"
API = "https://ssl.bing.com/webmaster/api.svc/json/"
KEY_ENV = "BING_WEBMASTER_API_KEY"
TIMEOUT = 30


class BingReportError(ValueError):
    """Safe collector error: never contains the API key or a provider body."""

    rate_limited = False


def _url(method, site_url, key):
    params = {} if method == "GetUserSites" else {"siteUrl": site_url}
    return API + method + "?" + urlencode({**params, "apikey": key})


def redact(text, key):
    """Remove the key wherever it appears, including any apikey= parameter."""
    text = str(text)
    if key:
        text = text.replace(key, "REDACTED")
    return re.sub(r"(apikey=)[^&\s\"']+", r"\1REDACTED", text, flags=re.IGNORECASE)


def fetch(method, site_url, key, opener=urllib.request.urlopen):
    request = urllib.request.Request(
        _url(method, site_url, key), headers={"Accept": "application/json"}
    )
    try:
        with opener(request, timeout=TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        error = BingReportError(
            f"Bing {method} rate-limited (HTTP 429)"
            if exc.code == 429
            else f"Bing {method} HTTP {exc.code}"
        )
        error.rate_limited = exc.code == 429
        raise error from None
    except (OSError, ValueError) as exc:
        # Transport messages can echo the request URL; keep only the type.
        raise BingReportError(
            f"Bing {method} request failed: {type(exc).__name__}"
        ) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise BingReportError(f"Bing {method} returned invalid JSON") from None
    if not isinstance(data, dict) or "d" not in data:
        raise BingReportError(f"Bing {method} response lacks 'd'")
    return data["d"]


def bing_date(value):
    """Parse WCF JSON dates such as /Date(1727654400000)/ or /Date(1727654400000-0700)/."""
    match = re.fullmatch(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/", str(value or ""))
    if not match:
        raise BingReportError("Bing returned an unparseable date")
    return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc).date()  # noqa: UP017


def _rows(method, value):
    if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
        raise BingReportError(f"Bing {method} returned non-list data")
    return value


def _number(row, field):
    value = row.get(field)
    if type(value) not in (int, float) or value < 0:
        raise BingReportError(f"Bing returned invalid {field}")
    return value


def collect(site_url, clock, key, opener=urllib.request.urlopen):
    """Return (observation, evidence). Raises BingReportError on any failure."""
    if not key:
        raise BingReportError(f"{KEY_ENV} is not set; Bing source enabled but not collected")
    evidence = []

    def call(method):
        value = fetch(method, site_url, key, opener)
        evidence.append(
            {"method": method, "url": redact(_url(method, site_url, key), key), "d": value}
        )
        return value

    sites = _rows("GetUserSites", call("GetUserSites"))
    wanted = site_url.rstrip("/").lower()
    site = next((s for s in sites if str(s.get("Url", "")).rstrip("/").lower() == wanted), None)
    if site is None:
        raise BingReportError("Bing GetUserSites does not list the configured site")
    verified = site.get("IsVerified") is True

    limitations, findings, rankings = [], [], []
    if not verified:
        findings.append("Bing lists the site as unverified; re-verify before reading data.")

    crawl = _rows("GetCrawlStats", call("GetCrawlStats"))
    queries = _rows("GetQueryStats", call("GetQueryStats"))
    traffic = _rows("GetRankAndTrafficStats", call("GetRankAndTrafficStats"))
    quota = call("GetUrlSubmissionQuota")
    if not isinstance(quota, dict):
        raise BingReportError("Bing GetUrlSubmissionQuota returned invalid data")

    metrics = {
        "site_verified": 1 if verified else 0,
        "url_submission_daily_quota": _number(quota, "DailyQuota"),
        "url_submission_monthly_quota": _number(quota, "MonthlyQuota"),
    }
    for name, rows in (("crawl", crawl), ("query", queries), ("rank/traffic", traffic)):
        if not rows:
            limitations.append(
                f"Bing returned no {name} rows: no data yet for this site. Unknown, not a measured zero."
            )

    if traffic:
        days = sorted(bing_date(r.get("Date")) for r in traffic)
        period_start, period_end = days[0], days[-1]
        metrics["impressions"] = sum(_number(r, "Impressions") for r in traffic)
        metrics["clicks"] = sum(_number(r, "Clicks") for r in traffic)
    else:
        period_start = period_end = clock.date()
        metrics["impressions"] = metrics["clicks"] = None
    metrics["rank_traffic_days"] = len(traffic)

    if crawl:
        latest = max(crawl, key=lambda r: bing_date(r.get("Date")))
        for field, metric in (
            ("CrawledPages", "crawled_pages"),
            ("CrawlErrors", "crawl_errors"),
            ("InIndex", "in_index"),
            ("InLinks", "in_links"),
        ):
            metrics[metric] = _number(latest, field) if field in latest else None
    else:
        for metric in ("crawled_pages", "crawl_errors", "in_index", "in_links"):
            metrics[metric] = None
    metrics["query_rows"] = len(queries)

    for row in queries:
        rankings.append(
            {
                "kind": "query",
                "key": str(row.get("Query", "")),
                "clicks": _number(row, "Clicks"),
                "impressions": _number(row, "Impressions"),
                "average_impression_position": row.get("AvgImpressionPosition"),
            }
        )
    limitations.append(
        "Bing reports Bing (and Bing-powered) search only; never add it to GSC totals. Crawl metrics are Bing's latest daily row; query rows are Bing's returned list, not every query."
    )
    observation = {
        "version": 1,
        "collector": COLLECTOR,
        "source": "bing",
        "property": site_url,
        "captured_at": clock.isoformat(),
        "period_start": str(period_start),
        "period_end": str(period_end),
        "scope": "Bing Webmaster API: site verification, crawl stats, query stats, rank/traffic stats, URL submission quota",
        "coverage": "partial",
        "limitations": limitations,
        "findings": findings,
        "metrics": metrics,
        "rankings": rankings,
    }
    return observation, evidence


def collect_source(cfg, state, clock, monitor, opener=urllib.request.urlopen):
    """Monitor hook: at most one Bing call per UTC day; failures keep the last good reading.

    The gate is the last attempt that reached Bing, successful or not, so a rerun after
    a 429 or 5xx on the same day does not call Bing again; that failed attempt stays in
    bing-attempt.json and the report until the next day's attempt. A missing key makes no
    request, so adding the key and rerunning the same day is allowed. Delete
    bing-attempt.json to force a deliberate same-day retry.
    """
    spec = cfg["sources"].get("bing")
    if not spec or not spec.get("bing_api", False):
        return
    attempt_path = state / "bing-attempt.json"
    key = os.environ.get(KEY_ENV, "")  # governance: allow-silent SF003: an unset key is handled: collect() raises BingReportError and the attempt is recorded as an error with called_bing False
    attempt = {"attempted_at": clock.isoformat(), "status": "running", "called_bing": bool(key)}
    path = state / "bing.json"
    try:
        if attempt_path.exists():
            last = monitor.read(attempt_path)
            if (
                last.get("called_bing", True)
                and monitor.timestamp(last["attempted_at"]).date() == clock.date()
            ):
                return
        if path.exists():
            previous = monitor.validate(monitor.read(path), cfg, clock)
            if (
                previous.get("collector") == COLLECTOR
                and monitor.timestamp(previous["captured_at"]).date() == clock.date()
            ):
                return
        monitor.write(attempt_path, attempt)
        observation, evidence = collect(spec["property"], clock, key, opener)
        evidence_path = state / "bing" / (clock.strftime("%Y%m%dT%H%M%SZ") + ".json")
        monitor.write(evidence_path, json.loads(redact(json.dumps(evidence), key)))
        monitor.save_observation(observation, cfg, state, evidence_path, clock)
        attempt["status"] = "success"
    except Exception as exc:  # noqa: BLE001 - independent collector boundary
        attempt.update(
            status="error",
            error=redact(exc, key)
            if isinstance(exc, BingReportError)
            else type(exc).__name__,
        )
        if getattr(exc, "rate_limited", False):
            attempt["rate_limited"] = True
    monitor.write(attempt_path, attempt)
