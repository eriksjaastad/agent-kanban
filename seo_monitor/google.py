"""Read-only GSC/GA4 report adapters. No account setup, writes or paid providers.

Google dependencies are optional for browser-import-only users. Requests are bounded;
API row limits, anonymization and GA4 metadata are retained, never called full coverage.
"""

import json
import os
from datetime import timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/analytics.readonly",
]
COLLECTOR = "google-api-v1"


class GoogleReportError(ValueError):
    """Safe collector error containing no credentials or provider response body."""


def session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["SEO_GOOGLE_CREDENTIALS"]), scopes=SCOPES
    )
    return AuthorizedSession(credentials, refresh_timeout=30, max_refresh_attempts=1)


def report_day(clock):
    return clock.astimezone(ZoneInfo("America/Los_Angeles")).date()


def collect(source, property_id, clock, post):
    """Fetch current/previous equal 28-day windows, with three days of reporting lag."""
    end = report_day(clock) - timedelta(days=3)
    start = end - timedelta(days=27)
    prior_end, prior_start = start - timedelta(days=1), start - timedelta(days=28)
    evidence = []

    def request(url, body):
        response = post(url, json=body, timeout=30)
        if response.status_code != 200:
            # Never persist request headers, credentials, or provider error bodies.
            raise GoogleReportError(f"{source} API HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict) or "error" in data:
            raise GoogleReportError(f"{source} API returned invalid report")
        evidence.append({"url": url, "request": body, "response": data})
        return data

    limitations = []
    rankings = []
    if source == "gsc":
        url = (
            "https://www.googleapis.com/webmasters/v3/sites/"
            + quote(property_id, safe="")
            + "/searchAnalytics/query"
        )

        def report(first, last, dimension=None):
            body = {
                "startDate": str(first),
                "endDate": str(last),
                "type": "web",
                "dataState": "final",
                "rowLimit": 1000,
            }
            if dimension:
                body["dimensions"] = [dimension]
            else:
                body["aggregationType"] = "byProperty"
            data = request(url, body)
            rows = data.get("rows", [])
            if not isinstance(rows, list):
                raise GoogleReportError("invalid GSC rows")
            return rows

        def totals(first, last):
            rows = report(first, last)
            if len(rows) > 1:
                raise GoogleReportError("GSC property total has multiple rows")
            if not rows:
                return {
                    "clicks": 0,
                    "impressions": 0,
                    "ctr_percent": None,
                    "average_position": None,
                }
            r = rows[0]
            return {
                "clicks": r["clicks"],
                "impressions": r["impressions"],
                "ctr_percent": r["ctr"] * 100,
                "average_position": r["position"],
            }

        metrics, previous = totals(start, end), totals(prior_start, prior_end)
        for dimension in ("query", "page"):
            rows = report(start, end, dimension)
            for row in rows:
                rankings.append(
                    {
                        "kind": dimension,
                        "key": row["keys"][0],
                        "clicks": row["clicks"],
                        "impressions": row["impressions"],
                        "average_position": row["position"],
                    }
                )
            if len(rows) == 1000:
                limitations.append(
                    f"{dimension} report reached the requested 1000-row limit."
                )
        limitations.append(
            "Finalized Web results only; queries omit anonymized searches and API returns top rows, not every row. Property/page/query aggregations differ; never sum them into a property total. No impressions means CTR/position are undefined."
        )
        scope = "GSC Web, all countries/devices; property totals; top1000 query and page rows; Pacific calendar dates; finalized data"
    elif source == "ga4":
        if not str(property_id).isdigit():
            raise GoogleReportError("GA4 property must be numeric")
        url = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"

        def report(first, last, organic=False, pages=False):
            body = {
                "dateRanges": [{"startDate": str(first), "endDate": str(last)}],
                "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}],
                "limit": "1000",
                "returnPropertyQuota": True,
            }
            if organic:
                body["dimensionFilter"] = {
                    "filter": {
                        "fieldName": "sessionDefaultChannelGroup",
                        "stringFilter": {
                            "matchType": "EXACT",
                            "value": "Organic Search",
                        },
                    }
                }
            if pages:
                body["dimensions"] = [{"name": "landingPagePlusQueryString"}]
                body["orderBys"] = [
                    {"metric": {"metricName": "sessions"}, "desc": True}
                ]
            data = request(url, body)
            rows = data.get("rows", [])
            empty_report = (
                not rows
                and not data.get("rowCount")
                and data.get("kind") == "analyticsData#runReport"
            )
            # GA4 omits headers entirely on legitimate empty reports.
            if not (empty_report and "metricHeaders" not in data) and [
                h["name"] for h in data["metricHeaders"]
            ] != ["sessions", "engagedSessions"]:
                raise GoogleReportError("unexpected GA4 metric headers")
            if not isinstance(rows, list):
                raise GoogleReportError("invalid GA4 rows")
            metadata = data.get("metadata", {})
            if (
                metadata.get("subjectToThresholding")
                or metadata.get("samplingMetadatas")
                or metadata.get("dataLossFromOtherRow")
            ):
                limitations.append(
                    "GA4 reports thresholding, sampling or aggregation loss; inspect saved response metadata before interpreting totals or changes."
                )
            if int(data.get("rowCount", len(rows))) > len(rows):
                limitations.append(
                    "GA4 returned fewer rows than rowCount; landing-page list is incomplete."
                )
            return rows, metadata

        def metric(rows, metadata, index):
            if rows:
                return float(rows[0]["metricValues"][index]["value"])
            # Suppressed empty responses do not establish a measured zero.
            return None if metadata.get("subjectToThresholding") else 0

        def totals(first, last):
            rows, meta = report(first, last)
            organic, organic_meta = report(first, last, organic=True)
            if len(rows) > 1 or len(organic) > 1:
                raise GoogleReportError("GA4 total has multiple rows")
            return {
                "sessions": metric(rows, meta, 0),
                "engaged_sessions": metric(rows, meta, 1),
                "organic_search_sessions": metric(organic, organic_meta, 0),
            }

        metrics, previous = totals(start, end), totals(prior_start, prior_end)
        pages, metadata = report(start, end, organic=True, pages=True)
        for row in pages:
            rankings.append(
                {
                    "kind": "organic_landing_page",
                    "key": row["dimensionValues"][0]["value"],
                    "sessions": float(row["metricValues"][0]["value"]),
                }
            )
        scope = f"GA4 all-session totals and Organic Search default-channel sessions; top1000 organic landing pages; property timezone {metadata.get('timeZone', 'not returned')}"
        limitations.append(
            "GA4 attribution and collection differ from GSC clicks; these are not interchangeable totals. Low traffic may include owner/bot activity. Three-day lag does not guarantee data will never be revised."
        )
    else:
        raise GoogleReportError("unsupported Google source")
    # Validate prior totals too; malformed/negative/NaN data cannot become a trend.
    import math

    for values in (metrics, previous):
        if any(
            v is not None
            and (type(v) not in (float, int) or not math.isfinite(v) or v < 0)
            for v in values.values()
        ):
            raise GoogleReportError("invalid Google metric")
    comparison = {
        "period_start": str(prior_start),
        "period_end": str(prior_end),
        "metrics": previous,
        "changes": {
            k: v - previous[k] if v is not None and previous[k] is not None else None
            for k, v in metrics.items()
        },
    }
    observation = {
        "version": 1,
        "collector": COLLECTOR,
        "source": source,
        "property": property_id,
        "captured_at": clock.isoformat(),
        "period_start": str(start),
        "period_end": str(end),
        "scope": scope,
        "coverage": "partial",
        "limitations": list(dict.fromkeys(limitations)),
        "findings": [],
        "metrics": metrics,
        "comparison": comparison,
        "rankings": rankings,
    }
    return observation, evidence
