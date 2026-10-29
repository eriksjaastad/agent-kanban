"""Slow GitHub requests must never hold up the dashboard or erase old data."""

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from dashboard.github_cache import GitHubCache


def settled(cache, fetch):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = cache.read(fetch)
        if not result["refreshing"]:
            return result
        time.sleep(0.005)
    pytest.fail("GitHub worker did not finish")


def test_slow_cold_fetch_returns_immediately_and_starts_only_one_worker():
    cache = GitHubCache()
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def slow():
        calls.append(True)
        entered.set()
        assert release.wait(2)
        return {"summary": {"total_repos": 4}}

    try:
        started = time.monotonic()
        result = cache.read(slow)
        assert time.monotonic() - started < 0.5
        assert result["refreshing"] and not result["cached"]
        assert "summary" not in result
        assert entered.wait(1)
        for _ in range(10):
            assert cache.read(slow)["refreshing"]
        assert len(calls) == 1
    finally:
        release.set()
    assert settled(cache, slow)["summary"]["total_repos"] == 4


def test_expired_snapshot_survives_failed_refresh_then_recovers():
    now = [100]
    cache = GitHubCache(ttl=10, retry_delay=5, clock=lambda: now[0])
    old = {"summary": {"total_repos": 4}, "fetched_at": "old"}
    assert settled(cache, lambda: old)["summary"] == old["summary"]
    now[0] = 111
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("private internal detail")

    failed = settled(cache, fail)
    assert failed["summary"] == old["summary"]
    assert failed["stale"] and failed["refresh_error"]
    assert "private" not in failed["refresh_error"]
    for _ in range(10):
        cache.read(fail)
    assert len(calls) == 1
    now[0] = 117
    fresh = settled(cache, lambda: {"summary": {"total_repos": 5}})
    assert fresh["summary"]["total_repos"] == 5
    assert not fresh["stale"] and fresh["refresh_error"] is None


@pytest.mark.parametrize("response", [{"error": "not authenticated"}, None])
def test_cold_failure_has_no_fake_empty_totals(response):
    cache = GitHubCache()
    result = settled(cache, lambda: response)
    assert result["refresh_error"]
    assert not result["cached"]
    assert "summary" not in result


def test_thread_start_failure_is_retryable():
    cache = GitHubCache()
    with patch("dashboard.github_cache.threading.Thread.start", side_effect=RuntimeError):
        result = cache.read(lambda: {})
    assert result["refresh_error"]
    assert not result["refreshing"]


def test_navigation_responds_while_github_collects():
    from dashboard.app import api_github, api_navigation

    cache = GitHubCache()
    release = threading.Event()

    def slow():
        assert release.wait(2)
        return {"summary": {}}

    async def requests():
        github = await api_github()
        navigation = await api_navigation()
        assert github["refreshing"]
        assert navigation

    try:
        with patch("dashboard.app._github_cache", cache), patch("dashboard.app._fetch_github_data", slow):
            asyncio.run(asyncio.wait_for(requests(), timeout=0.5))
    finally:
        release.set()
    settled(cache, slow)
