"""Discord ping when a calendar block starts (#8795). Nothing here reaches Doppler or Discord."""

import json
import logging
import subprocess
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


CONFIG = ('[discord]\nchannel_id = "4242"\ndoppler_project = "bots"\ndoppler_config = "prd"\n'
          'token_secret = "BOT_TOKEN"\n')
TOKEN = "s3cret-token-value"


class Reply:
    def __init__(self, status=200, body=None):
        self.status = status
        self._body = json.dumps({"id": "9", "channel_id": "4242"} if body is None else body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def discord(tmp_path, monkeypatch):
    """A [discord] config, a stubbed Doppler and a stubbed urlopen: no real network or Doppler."""
    (tmp_path / "c.toml").write_text(CONFIG)
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "c.toml"))
    seen = {"doppler": [], "requests": [], "reply": Reply()}

    def run(command, **kwargs):
        seen["doppler"].append(command)
        return subprocess.CompletedProcess(command, 0, stdout=TOKEN + "\n")

    def urlopen(request, timeout):
        seen["requests"].append(request)
        return seen["reply"]

    monkeypatch.setattr(block_ping.subprocess, "run", run)
    monkeypatch.setattr(block_ping.urllib.request, "urlopen", urlopen)
    return seen


def test_post_goes_through_the_bot_to_the_configured_channel(discord):
    block_ping.post("Deep work now, until 10:30")
    (request,) = discord["requests"]
    assert request.full_url == "https://discord.com/api/v10/channels/4242/messages"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bot {TOKEN}"
    assert request.get_header("User-agent").startswith("DiscordBot (")
    assert json.loads(request.data) == {"content": "Deep work now, until 10:30",
                                        "allowed_mentions": {"parse": []}}
    assert discord["doppler"] == [["doppler", "secrets", "get", "BOT_TOKEN", "--plain",
                                   "--project", "bots", "--config", "prd"]]


@pytest.mark.parametrize("reply", [
    Reply(204, {}), Reply(200, {"channel_id": "4242"}), Reply(200, {"id": "9", "channel_id": "other"}),
    Reply(200, []), Reply(200, "not an object"),
])
def test_an_unconfirmed_response_raises(discord, reply):
    discord["reply"] = reply
    with pytest.raises(RuntimeError, match="did not confirm"):
        block_ping.post("x")


def test_an_unconfigured_discord_says_how_to_turn_it_on(tmp_path, monkeypatch):
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "none.toml"))
    with pytest.raises(RuntimeError, match=r"Discord is not configured: add a \[discord\] section"):
        block_ping.post("x")


def test_the_token_never_reaches_an_error_or_a_log(discord, monkeypatch, caplog, sent):
    def failing(command, **kwargs):
        raise subprocess.CalledProcessError(2, command, output=TOKEN, stderr=TOKEN)

    monkeypatch.setattr(block_ping.subprocess, "run", failing)
    with pytest.raises(RuntimeError) as raised:
        block_ping.post("x")
    assert TOKEN not in str(raised.value) and raised.value.__cause__ is None
    discord["reply"] = Reply(500, {"message": TOKEN})
    monkeypatch.setattr(block_ping.subprocess, "run",
                        lambda c, **k: subprocess.CompletedProcess(c, 0, stdout=TOKEN))
    with caplog.at_level(logging.ERROR):
        block_ping.check(START, _block(), block_ping.post, sent[1])
    assert "did not confirm" in caplog.text and TOKEN not in caplog.text


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
