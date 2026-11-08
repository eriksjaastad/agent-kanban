"""Compare Screaming Frog Internal:All exports from the same configuration.

Heuristic on-page/link warnings are investigation leads, not search-engine rules.
With --after-log, classify coverage from the completed fetch count. Incomplete
coverage suppresses absence claims but retains observed URL regressions.
Exit 1: observed changes requiring investigation; 2: incomplete without such
changes; 0: neither. Callers omitting coverage attest that scope is comparable.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from pathlib import Path

HTML_CONTENT_TYPE = "text/html"

# Screening heuristics; low counts alone do not establish an SEO defect.
MIN_UNIQUE_INLINKS = 2

# This is a review threshold, not a search-engine minimum word count.
MIN_WORD_COUNT = 200

Rows = dict[str, dict[str, str]]


def load_export(path: Path) -> Rows:
    """Read a Screaming Frog CSV export, keyed by URL.

    Screaming Frog writes a UTF-8 BOM; ``utf-8-sig`` strips it so the first
    column header is ``Address`` and not ``﻿Address``.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or "Address" not in reader.fieldnames:
            raise ValueError(
                f"{path} does not look like a Screaming Frog Internal:All export "
                "(no 'Address' column)"
            )
        return {row["Address"]: row for row in reader}


def html_pages(rows: Rows) -> Rows:
    """Keep only HTML documents — images and JSON are not SEO surfaces."""
    return {
        url: row
        for url, row in rows.items()
        if HTML_CONTENT_TYPE in (row.get("Content Type") or "")
    }


def as_int(value: str | None) -> int:
    """Coerce a Screaming Frog numeric cell to an int, defaulting to 0.

    A blank cell genuinely means zero. A cell holding something unparseable
    does not, and reading it as zero would flag the page as orphaned or thin
    on no evidence — so say so on stderr rather than quietly inventing a
    number.
    """
    if value is None or not value.strip():
        return 0
    try:
        return int(value)
    except ValueError:  # governance: allow-silent SF002: SEO report helper; an unparseable cell warns on stderr and 0 errs toward flagging the page for review, never hiding it (test_an_unparseable_cell_warns_rather_than_inventing_a_number)
        print(
            f"warning: unparseable numeric cell {value!r}, reading as 0",
            file=sys.stderr,
        )
        return 0


def display_path(path: Path) -> str:
    """Render a path relative to the repo when possible.

    The diff output is committed under seo-audits/, so an absolute path would
    bake whichever machine ran the crawl into the tracked record.
    """
    try:
        return str(path.resolve().relative_to(Path(__file__).resolve().parent.parent))
    except ValueError:
        return path.name


def find_regressions(before: Rows, after: Rows, *, complete: bool = True) -> list[str]:
    """Report only changes that are unambiguously worse or need explaining.

    New pages are not regressions — the daily pipeline publishes new dossiers
    by design. A page that *disappears* is the opposite: a published dossier
    should never stop being crawlable, so vanishing counts here and drives the
    exit code.
    """
    regressions: list[str] = []

    # Checked before the intersection below, because a page that vanished is
    # by definition not in it. This is the failure mode the weekly run exists
    # to catch, and the one an intersection-only loop silently misses.
    for url in sorted(set(before) - set(after)):
        if complete and before[url].get("Indexability") == "Indexable":
            regressions.append(f"{url}\n    was indexable, now absent from the crawl")

    for url in sorted(set(before) & set(after)):
        was, now = before[url], after[url]

        if was.get("Status Code") != now.get("Status Code"):
            regressions.append(
                f"{url}\n    status {was.get('Status Code')} -> {now.get('Status Code')}"
            )

        if was.get("Indexability") != now.get("Indexability"):
            reason = now.get("Indexability Status") or "no reason given"
            regressions.append(
                f"{url}\n    indexability {was.get('Indexability')} -> "
                f"{now.get('Indexability')} ({reason})"
            )

        was_canonical = (was.get("Canonical Link Element 1") or "").strip()
        now_canonical = (now.get("Canonical Link Element 1") or "").strip()
        if was_canonical != now_canonical:
            regressions.append(
                f"{url}\n    canonical {was_canonical or '(none)'} -> "
                f"{now_canonical or '(none)'}"
            )

    return regressions


def thin_inlinks(rows: Rows) -> list[tuple[str, int]]:
    """Indexable HTML pages reachable from fewer than MIN_UNIQUE_INLINKS pages."""
    return sorted(
        (url, as_int(row.get("Unique Inlinks")))
        for url, row in rows.items()
        if row.get("Indexability") == "Indexable"
        and as_int(row.get("Unique Inlinks")) < MIN_UNIQUE_INLINKS
    )


def on_page_gaps(rows: Rows) -> list[tuple[str, list[str]]]:
    """Indexable HTML pages missing a title, a meta description, or substance."""
    gaps: list[tuple[str, list[str]]] = []
    for url, row in sorted(rows.items()):
        if row.get("Indexability") != "Indexable":
            continue
        problems: list[str] = []
        if not (row.get("Title 1") or "").strip():
            problems.append("no title")
        if not (row.get("Meta Description 1") or "").strip():
            problems.append("no meta description")
        word_count = as_int(row.get("Word Count"))
        if word_count < MIN_WORD_COUNT:
            problems.append(f"thin: {word_count} words")
        if problems:
            gaps.append((url, problems))
    return gaps


def _print_count_shift(before: Rows, after: Rows, field: str) -> None:
    was, now = (
        Counter(r.get(field, "") for r in before.values()),
        Counter(r.get(field, "") for r in after.values()),
    )
    for key in sorted(set(was) | set(now)):
        delta = now[key] - was[key]
        suffix = f"  ({delta:+d})" if delta else ""
        print(f"  {key or '(blank)':<28} {was[key]:>4} -> {now[key]:>4}{suffix}")


def crawl_coverage(log: str, limit: int) -> tuple[str, int | None]:
    """Completed means within configured scope, never all URLs on the website."""
    # List mode (--crawl-sitemap) continues the record over several lines (the
    # sitemap, then one start URL per line, then "and N others ... crawled N urls").
    # Match within one log record: never past a line that starts a new timestamp.
    counts = re.findall(
        r"Completed the spider of (?:(?!\n\d{4}-\d{2}-\d{2} )[\s\S])*?crawled (\d+) urls",
        log,
        re.IGNORECASE,
    )
    if not counts:
        return "unknown", None
    count = int(counts[-1])
    return ("capped" if count >= limit else "complete"), count


def report(before_path: Path, after_path: Path, *, complete: bool = True) -> int:
    before, after = load_export(before_path), load_export(after_path)
    before_html, after_html = html_pages(before), html_pages(after)

    print(
        f"# SEO crawl diff\n\nbefore: {display_path(before_path)}\n"
        f"after:  {display_path(after_path)}"
    )

    print("\n## Crawl size")
    print(
        f"  all URLs    {len(before):>4} -> {len(after):>4}  ({len(after) - len(before):+d})"
    )
    print(
        f"  HTML pages  {len(before_html):>4} -> {len(after_html):>4}  "
        f"({len(after_html) - len(before_html):+d})"
    )

    print("\n## Status codes (all URLs)")
    _print_count_shift(before, after, "Status Code")

    print("\n## Indexability (HTML pages)")
    _print_count_shift(before_html, after_html, "Indexability")

    added, removed = sorted(set(after) - set(before)), sorted(set(before) - set(after))
    print(f"\n## URLs added ({len(added)})")
    print("\n".join(f"  + {url}" for url in added) or "  none")
    print(
        f"\n## URLs {'removed' if complete else 'not observed (incomplete crawl)'} ({len(removed)})"
    )
    print("\n".join(f"  - {url}" for url in removed) or "  none")

    regressions = find_regressions(before_html, after_html, complete=complete)
    print(f"\n## Regressions ({len(regressions)})")
    print("\n".join(f"  {line}" for line in regressions) or "  none")

    print(
        "\n## Thin internal linking (indexable pages under "
        f"{MIN_UNIQUE_INLINKS} unique inlinks)"
    )
    was_thin, now_thin = thin_inlinks(before_html), thin_inlinks(after_html)
    print(f"  {len(was_thin)} -> {len(now_thin)}  ({len(now_thin) - len(was_thin):+d})")
    for url, count in now_thin:
        print(f"      {url}  ({count} inlinks)")

    gaps = on_page_gaps(after_html)
    print(f"\n## On-page gaps on indexable pages ({len(gaps)})")
    for url, problems in gaps:
        print(f"  {url}\n      {', '.join(problems)}")

    return 1 if regressions else (0 if complete else 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Diff two Screaming Frog Internal:All CSV exports."
    )
    parser.add_argument("before", type=Path, help="earlier Internal:All CSV export")
    parser.add_argument("after", type=Path, help="later Internal:All CSV export")
    parser.add_argument("--after-log", type=Path)
    parser.add_argument("--crawl-limit", type=int, default=500)
    args = parser.parse_args(argv)
    complete = True
    if args.after_log:
        coverage, count = crawl_coverage(args.after_log.read_text(), args.crawl_limit)
        complete = coverage == "complete"
        print(f"Coverage: {coverage}; fetched: {count}; limit: {args.crawl_limit}")
        if not complete:
            print("Incomplete coverage: unobserved URLs are not confirmed removals.")
    return report(args.before, args.after, complete=complete)


if __name__ == "__main__":
    sys.exit(main())
