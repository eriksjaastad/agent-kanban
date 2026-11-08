"""Read-only PageSpeed Insights v5 adapter for page-type performance (standard library only).

https://developers.google.com/speed/docs/insights/v5/get-started

Lighthouse lab data from Google's PSI servers (simulated throttling), never real-user
data. The same URL measured minutes apart swings widely, so each configured URL and
strategy is run N times and the median, min and max of every metric are kept.
Accessibility and SEO audits are deterministic; their failing audit ids are findings.

Field data is whatever PSI returns in loadingExperience (CrUX). An empty block means
the page is below CrUX's traffic threshold: recorded as unknown (null), never zero.
The Chrome UX Report API itself requires a Google Cloud API key; the service account's
OAuth token is rejected there, so it is not queried.

Auth: an OAuth token from the SEO_GOOGLE_CREDENTIALS service account, sent only in the
Authorization header (anonymous PSI is rate-limited to HTTP 429). Evidence and errors
never contain it. Bounded: pages x strategies x runs requests, 180s timeout each, small
thread pool, no retries; one URL's failure is recorded for that URL and the others
continue, while HTTP 429 stops the whole run.
"""

import hashlib
import json
import os
import statistics
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import unquote, urlencode, urlparse

COLLECTOR = "pagespeed-insights-v5"
API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
CREDENTIAL_ENV = "SEO_GOOGLE_CREDENTIALS"
# PSI accepts a service-account token with no API-specific scope.
SCOPES = ["openid"]
TIMEOUT = 180
TOKEN_TIMEOUT = 30
CATEGORIES = ("performance", "accessibility", "seo", "best-practices")
STRATEGIES = ("mobile", "desktop")
MAX_RUNS = 5
MAX_CONCURRENCY = 4
# Lighthouse audit id -> recorded metric (ms, except unitless CLS).
LAB_METRICS = {
    "largest-contentful-paint": "lcp_ms",
    "first-contentful-paint": "fcp_ms",
    "total-blocking-time": "tbt_ms",
    "cumulative-layout-shift": "cls",
    "speed-index": "speed_index_ms",
}
# CrUX metric key in loadingExperience -> recorded 75th percentile.
FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": "lcp_p75_ms",
    "INTERACTION_TO_NEXT_PAINT": "inp_p75_ms",
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": "cls_p75",
    "FIRST_CONTENTFUL_PAINT_MS": "fcp_p75_ms",
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": "ttfb_p75_ms",
}
# Audits that can fail; manual/informative/notApplicable ones carry no verdict.
SCORED_MODES = {"binary", "numeric", "metricSavings"}


class PageSpeedError(ValueError):
    """Safe collector error: never contains the token or a provider body."""

    rate_limited = False


def redact(text, token):
    text = str(text)
    return text.replace(token, "REDACTED") if token else text


def validate_config(spec, site):
    """Reject unbounded or off-site configuration before any request is made."""
    host = urlparse(site).hostname
    pages = spec.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("pagespeed pages must be a non-empty list")
    types = [p.get("type") for p in pages]
    urls = [p.get("url") for p in pages]
    if not all(isinstance(t, str) and t for t in types) or len(set(types)) != len(types):
        raise ValueError("pagespeed page types must be unique non-empty strings")
    if len(set(urls)) != len(urls) or not all(
        isinstance(u, str) and urlparse(u).scheme in {"http", "https"} and urlparse(u).hostname == host
        for u in urls
    ):
        raise ValueError("pagespeed pages must be unique URLs on the configured site")
    strategies = spec.get("strategies", list(STRATEGIES))
    if not strategies or not set(strategies) <= set(STRATEGIES) or len(set(strategies)) != len(strategies):
        raise ValueError("pagespeed strategies must be mobile and/or desktop")
    if not (
        type(spec.get("runs")) is int and 1 <= spec["runs"] <= MAX_RUNS
        and type(spec.get("concurrency")) is int and 1 <= spec["concurrency"] <= MAX_CONCURRENCY
        and type(spec.get("interval_days")) is int and spec["interval_days"] > 0
    ):
        raise ValueError(
            f"pagespeed runs (1-{MAX_RUNS}), concurrency (1-{MAX_CONCURRENCY}) and interval_days (>0) required"
        )


def fingerprint(spec):
    """Pages, strategies and runs define comparability; a change re-measures at once."""
    keys = {k: spec.get(k) for k in ("pages", "strategies", "runs")}
    return hashlib.sha256(json.dumps(keys, sort_keys=True).encode()).hexdigest()


def access_token():
    """Mint a short-lived OAuth token from the service account; fail loudly if unset."""
    info = os.environ.get(CREDENTIAL_ENV, "")  # governance: allow-silent SF003: an unset credential raises PageSpeedError below and the attempt is recorded as an error with called_api False
    if not info:
        raise PageSpeedError(f"{CREDENTIAL_ENV} is not set; PageSpeed source enabled but not collected")
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(info), scopes=SCOPES
    )
    transport = Request()

    def bounded(**kwargs):
        kwargs["timeout"] = TOKEN_TIMEOUT
        return transport(**kwargs)

    credentials.refresh(bounded)
    return credentials.token


def _url(page_url, strategy):
    return API + "?" + urlencode(
        [("url", page_url), ("strategy", strategy)] + [("category", c) for c in CATEGORIES]
    )


def fetch(page_url, strategy, token, opener=urllib.request.urlopen):
    request = urllib.request.Request(
        _url(page_url, strategy),
        headers={"Accept": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with opener(request, timeout=TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        error = PageSpeedError(
            "PageSpeed rate-limited (HTTP 429)" if exc.code == 429 else f"PageSpeed HTTP {exc.code}"
        )
        error.rate_limited = exc.code == 429
        raise error from None
    except (OSError, ValueError) as exc:
        # Transport messages can echo request headers; keep only the type.
        raise PageSpeedError(f"PageSpeed request failed: {type(exc).__name__}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise PageSpeedError("PageSpeed returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("lighthouseResult"), dict):
        raise PageSpeedError("PageSpeed response lacks lighthouseResult")
    return data


def _score(value):
    return value if type(value) in (int, float) and 0 <= value <= 1 else None


def _failing(result, category):
    refs = result.get("categories", {}).get(category, {}).get("auditRefs", [])
    audits = result.get("audits", {})
    failing = []
    for ref in refs:
        audit = audits.get(ref.get("id"), {})
        score = audit.get("score")
        if audit.get("scoreDisplayMode") in SCORED_MODES and type(score) in (int, float) and score < 1:
            failing.append(ref["id"])
    return failing


def _field(experience):
    """CrUX block from PSI: absent metrics are unknown (None), never zero."""
    metrics = experience.get("metrics") if isinstance(experience, dict) else None
    metrics = metrics if isinstance(metrics, dict) else {}
    field = {"available": bool(metrics), "origin_fallback": bool(experience.get("origin_fallback")) if isinstance(experience, dict) else False}
    field["overall_category"] = experience.get("overall_category") if metrics else None
    for key, name in FIELD_METRICS.items():
        value = (metrics.get(key) or {}).get("percentile")
        if type(value) not in (int, float) or value < 0:
            value = None
        elif key == "CUMULATIVE_LAYOUT_SHIFT_SCORE":
            value = value / 100  # CrUX reports CLS x 100
        field[name] = value
    return field


def _same_page(requested, final):
    """True when PSI measured the configured page: an http/https, www. or trailing-slash
    redirect is the same page; any other destination is not (#7533 review)."""
    a, b = urlparse(requested), urlparse(str(final or ""))

    def host(u):
        return (u.hostname or "").lower().removeprefix("www.")

    def path(u):
        return unquote(u.path).rstrip("/")

    return bool(b.hostname) and b.scheme in ("http", "https") and host(a) == host(b) and path(a) == path(b)


def summarize_run(data, requested_url):
    """One PSI response -> lab readings. Raises PageSpeedError when Lighthouse failed,
    measured a different page, or omitted a requested category or lab metric: a partial
    reading must not count as a successful run and hold off the next one for a week."""
    result = data["lighthouseResult"]
    runtime = result.get("runtimeError")
    if isinstance(runtime, dict) and runtime.get("code"):
        raise PageSpeedError(f"Lighthouse runtime error {runtime['code']}")
    if not _same_page(requested_url, result.get("finalUrl")):
        final = urlparse(str(result.get("finalUrl") or ""))
        raise PageSpeedError(
            f"PageSpeed measured a different page ({final.netloc}{final.path or '/'}); "
            "the configured URL no longer represents its page type"
            if final.netloc
            else "PageSpeed result lacks finalUrl"
        )
    categories = result.get("categories", {})
    run = {
        "performance": _score(categories.get("performance", {}).get("score")),
        "accessibility": _score(categories.get("accessibility", {}).get("score")),
        "seo": _score(categories.get("seo", {}).get("score")),
        "best_practices": _score(categories.get("best-practices", {}).get("score")),
    }
    audits = result.get("audits", {})
    for audit, name in LAB_METRICS.items():
        value = audits.get(audit, {}).get("numericValue")
        run[name] = value if type(value) in (int, float) and value >= 0 else None
    missing = sorted(k for k, v in run.items() if v is None)
    if missing:
        raise PageSpeedError("Lighthouse result incomplete: missing " + ", ".join(missing))
    return {
        "lab": run,
        "failing_audits": {c: _failing(result, c) for c in ("accessibility", "seo")},
        "field": _field(data.get("loadingExperience")),
        "origin_field": _field(data.get("originLoadingExperience")),
        "evidence": {
            "analysisUTCTimestamp": data.get("analysisUTCTimestamp"),
            "lighthouseVersion": result.get("lighthouseVersion"),
            "requestedUrl": result.get("requestedUrl"),
            "finalUrl": result.get("finalUrl"),
            "fetchTime": result.get("fetchTime"),
            "configSettings": result.get("configSettings"),
            "environment": result.get("environment"),
            "runWarnings": result.get("runWarnings"),
            "lab": run,
            "loadingExperience": data.get("loadingExperience"),
            "originLoadingExperience": data.get("originLoadingExperience"),
        },
    }


def _spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"median": statistics.median(values), "min": min(values), "max": max(values)}


def collect(spec, clock, token, opener=urllib.request.urlopen):
    """Return (observation, evidence). Raises PageSpeedError on a 429 or total failure."""
    strategies = spec.get("strategies", list(STRATEGIES))
    runs = spec["runs"]
    # Repeat runs are interleaved, not back to back, so one bad minute at Google's
    # end lands on one run of many URLs rather than every run of one URL.
    tasks = [
        (index, position, strategy)
        for index in range(runs)
        for position in range(len(spec["pages"]))
        for strategy in strategies
    ]
    stop = threading.Event()
    outcomes = {}

    def measure(task):
        _, position, strategy = task
        if stop.is_set():
            return None
        try:
            data = fetch(spec["pages"][position]["url"], strategy, token, opener)
            try:
                return summarize_run(data, spec["pages"][position]["url"])
            except (AttributeError, KeyError, TypeError):
                # One malformed response is that URL's failed run, not the whole run's.
                raise PageSpeedError("PageSpeed returned a malformed lighthouseResult") from None
        except PageSpeedError as exc:
            if exc.rate_limited:
                stop.set()
            return exc

    with ThreadPoolExecutor(max_workers=spec["concurrency"]) as pool:
        for task, outcome in zip(tasks, pool.map(measure, tasks), strict=True):
            outcomes[task] = outcome
    if stop.is_set():
        error = PageSpeedError("PageSpeed rate-limited (HTTP 429); run stopped, last reading kept")
        error.rate_limited = True
        raise error

    pages, evidence, limitations, findings = [], [], [], []
    metrics = {"page_types": len(spec["pages"]), "runs_per_measurement": runs}
    succeeded = failed = with_field = 0
    for position, page in enumerate(spec["pages"]):
        for strategy in strategies:
            done = [outcomes[(i, position, strategy)] for i in range(runs)]
            good = [o for o in done if isinstance(o, dict)]
            errors = [str(o) for o in done if not isinstance(o, dict)]
            succeeded += len(good)
            failed += len(errors)
            evidence.append(
                {"type": page["type"], "url": page["url"], "strategy": strategy,
                 "runs": [o["evidence"] if isinstance(o, dict) else {"error": str(o)} for o in done]}
            )
            names = ("performance", "accessibility", "seo", "best_practices", *LAB_METRICS.values())
            record = {
                "type": page["type"],
                "url": page["url"],
                "strategy": strategy,
                "runs_requested": runs,
                "runs_succeeded": len(good),
                "errors": errors,
                "lab": {n: _spread(o["lab"][n] for o in good) for n in names},
                "failing_audits": {
                    c: sorted({a for o in good for a in o["failing_audits"][c]})
                    for c in ("accessibility", "seo")
                },
                # Field data describes real users over 28 days, not a run: take the first.
                "field": good[0]["field"] if good else _field(None),
                "origin_field": good[0]["origin_field"] if good else _field(None),
            }
            with_field += record["field"]["available"]
            if errors:
                limitations.append(
                    f"{page['type']} {strategy}: {len(errors)} of {runs} runs failed ({'; '.join(sorted(set(errors)))})."
                )
            for category, label in (("accessibility", "Accessibility"), ("seo", "SEO")):
                if record["failing_audits"][category]:
                    findings.append(
                        f"{label} audits failing on {page['type']} ({page['url']}) {strategy}: "
                        + ", ".join(record["failing_audits"][category])
                    )
            pages.append(record)
    if not succeeded:
        raise PageSpeedError("every PageSpeed run failed: " + "; ".join(sorted({e for p in pages for e in p["errors"]})))
    metrics.update(
        measurements=len(pages),
        runs_succeeded=succeeded,
        runs_failed=failed,
        measurements_with_field_data=with_field,
    )
    for strategy in strategies:
        medians = [p["lab"] for p in pages if p["strategy"] == strategy]
        perf = [m["performance"]["median"] for m in medians if m["performance"]]
        lcp = [m["lcp_ms"]["median"] for m in medians if m["lcp_ms"]]
        metrics[f"{strategy}_performance_worst_median"] = min(perf) if perf else None
        metrics[f"{strategy}_lcp_ms_worst_median"] = max(lcp) if lcp else None
    limitations.append(
        f"Lighthouse lab data (PSI servers, simulated throttling), not real-user data: median, min and max of {runs} runs per URL and strategy. Single runs swing widely; compare medians only, across the same settings."
    )
    limitations.append(
        "One representative URL per page type; other pages of the same type are not measured."
    )
    if with_field < len(pages):
        limitations.append(
            f"No CrUX field data for {len(pages) - with_field} of {len(pages)} measurements (traffic below CrUX thresholds): unknown, not zero."
        )
    limitations.append(
        "Chrome UX Report API not queried: it requires a Google Cloud API key and rejects the service account's OAuth token; field data comes only from PSI's loadingExperience."
    )
    observation = {
        "version": 1,
        "collector": COLLECTOR,
        "source": "pagespeed",
        "property": spec["property"],
        "captured_at": clock.isoformat(),
        "period_start": clock.date().isoformat(),
        "period_end": clock.date().isoformat(),
        "scope": f"PageSpeed Insights v5 Lighthouse lab: {len(spec['pages'])} page types x {'/'.join(strategies)} x {runs} runs; categories {', '.join(CATEGORIES)}",
        "coverage": "partial",
        "limitations": limitations,
        "findings": findings,
        "metrics": metrics,
        "pages": pages,
        "config_fingerprint": fingerprint(spec),
    }
    return observation, evidence


def report_lines(obs):
    """Compact per-page medians for report.md; full ranges stay in the JSON."""
    def value(lab, name, digits=0):
        spread = lab.get(name)
        return "n/a" if spread is None else (f"{spread['median']:.{digits}f}" if digits else str(round(spread["median"])))

    lines = ["Page-type medians (Lighthouse lab, simulated throttling; not real-user data):"]
    for page in obs.get("pages", []):
        lab = page["lab"]
        perf = lab.get("performance")
        lines.append(
            f"    {page['type']} {page['strategy']}: "
            + (f"perf {perf['median']:g} ({perf['min']:g}-{perf['max']:g})" if perf else "perf n/a")
            + f", LCP {value(lab, 'lcp_ms')}ms, FCP {value(lab, 'fcp_ms')}ms, TBT {value(lab, 'tbt_ms')}ms"
            + f", CLS {value(lab, 'cls', 3)}, SI {value(lab, 'speed_index_ms')}ms"
            + f", a11y {value(lab, 'accessibility', 2)}, seo {value(lab, 'seo', 2)}"
            + f"; field {page['field']['overall_category'] or 'unknown'}"
            + f"; runs {page['runs_succeeded']}/{page['runs_requested']}"
        )
    return lines


def collect_source(cfg, state, clock, monitor, opener=urllib.request.urlopen, token=access_token):
    """Monitor hook: a complete reading waits `interval_days`; failures keep the last good one.

    Like Bing, the gate is also the last attempt that reached PSI: a rerun after a 429 or
    total failure on the same UTC day does not call PSI again. A reading with failed runs
    is re-measured on the next day's run instead of waiting out the interval, and so is
    one taken with different pages, strategies or run count. A missing
    credential makes no request, so adding it and rerunning the same day is allowed.
    Delete pagespeed-attempt.json to force a deliberate same-day retry.
    """
    spec = cfg["sources"].get("pagespeed")
    if not spec or not spec.get("pagespeed_api", False):
        return
    attempt_path = state / "pagespeed-attempt.json"
    attempt = {"attempted_at": clock.isoformat(), "status": "running", "called_api": False}
    secret = ""
    try:
        if attempt_path.exists():
            last = monitor.read(attempt_path)
            if (
                last.get("called_api", True)
                and monitor.timestamp(last["attempted_at"]).date() == clock.date()
            ):
                return
        path = state / "pagespeed.json"
        if path.exists():
            previous = monitor.validate(monitor.read(path), cfg, clock)
            if (
                previous.get("collector") == COLLECTOR
                and previous.get("config_fingerprint") == fingerprint(spec)
                and previous["metrics"].get("runs_failed") == 0
                and clock - monitor.timestamp(previous["captured_at"]) < timedelta(days=spec["interval_days"])
            ):
                return
        monitor.write(attempt_path, attempt)
        secret = token()
        attempt["called_api"] = True
        monitor.write(attempt_path, attempt)
        observation, evidence = collect(spec, clock, secret, opener)
        evidence_path = state / "pagespeed" / (clock.strftime("%Y%m%dT%H%M%SZ") + ".json")
        monitor.write(evidence_path, json.loads(redact(json.dumps(evidence), secret)))
        monitor.save_observation(observation, cfg, state, evidence_path, clock)
        attempt["status"] = "success"
        attempt["runs_failed"] = observation["metrics"]["runs_failed"]
    except Exception as exc:  # noqa: BLE001 - independent collector boundary
        attempt.update(
            status="error",
            error=redact(exc, secret) if isinstance(exc, PageSpeedError) else type(exc).__name__,
        )
        if getattr(exc, "rate_limited", False):
            attempt["rate_limited"] = True
    monitor.write(attempt_path, attempt)
