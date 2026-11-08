"""Discord ping when a calendar block starts (#8795). Nothing here reaches Doppler or Discord."""

import json
import logging
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from dashboard import app as app_module
from dashboard import block_ping

START = datetime(2026, 11, 2, 9, 0)


def _block(start=START, project: str | None = "project-tracker", block_id=7):
    return {"id": block_id, "title": "Deep work", "project_id": project,
            "start": start, "end": start + timedelta(hours=1, minutes=30)}


@pytest.fixture
def sent(tmp_path):
    messages = []
    return messages, tmp_path / "data" / "block_pings.json"


def test_one_message_per_block_even_across_restarts(sent):
    messages, path = sent
    block_ping.check(START, _block(), messages.append, path)
    block_ping.check(START + timedelta(minutes=1), _block(), messages.append, path)  # same file = after a restart
    tomorrow = _block(start=START + timedelta(days=1))
    block_ping.check(tomorrow["start"], tomorrow, messages.append, path)  # a recurring block's next day
    block_ping.check(START, None, messages.append, path)
    assert messages == ["Deep work (project-tracker) now, until 10:30"] * 2


def test_a_block_without_a_project_and_a_late_block(sent):
    messages, path = sent
    block_ping.check(START + timedelta(minutes=9), _block(project=None), messages.append, path)
    late = _block(block_id=8)
    block_ping.check(START + timedelta(minutes=10), late, messages.append, path)
    assert messages == ["Deep work now, until 10:30"]


def test_a_failed_send_is_logged_once_and_not_retried(sent, caplog):
    _, path = sent
    calls = []

    def broken(text):
        calls.append(text)
        raise OSError("no route")

    with caplog.at_level(logging.ERROR):
        block_ping.check(START, _block(), broken, path)
        block_ping.check(START, _block(), broken, path)
    assert len(calls) == 1
    assert [r.levelno for r in caplog.records if "block ping failed" in r.message] == [logging.ERROR]
    assert json.loads(path.read_text()) == [f"7@{START.isoformat()}"]


def test_post_sends_the_text_as_discord_content(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(request, timeout):
        seen.update(url=request.full_url, body=json.loads(request.data), method=request.get_method())
        return Response()

    monkeypatch.setattr(block_ping, "webhook", lambda: "https://discord.test/api/webhooks/1/x")
    monkeypatch.setattr(block_ping.urllib.request, "urlopen", urlopen)
    block_ping.post("Deep work now, until 10:30")
    assert seen == {"url": "https://discord.test/api/webhooks/1/x",
                    "body": {"content": "Deep work now, until 10:30"}, "method": "POST"}


@pytest.mark.parametrize("flag, started", [(None, 0), ("1", 1)])
def test_only_the_launchd_server_starts_pinging(monkeypatch, flag, started):
    starts = []
    monkeypatch.setattr(block_ping, "start", lambda: starts.append(1))
    if flag is None:
        monkeypatch.delenv("PT_BLOCK_PINGS", raising=False)
    else:
        monkeypatch.setenv("PT_BLOCK_PINGS", flag)
    with TestClient(app_module.app):
        pass
    assert len(starts) == started


def test_a_write_cut_off_before_the_swap_keeps_the_old_list(sent, monkeypatch):
    messages, path = sent
    block_ping.check(START, _block(), messages.append, path)
    before = path.read_text()

    def killed(src, dst):
        raise KeyboardInterrupt  # the process dies after writing the new list, before swapping it in

    monkeypatch.setattr(block_ping.os, "replace", killed)
    with pytest.raises(KeyboardInterrupt):
        block_ping.check(START, _block(block_id=8), messages.append, path)
    assert path.read_text() == before
