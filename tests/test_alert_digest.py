"""Tests for scripts/alert_digest.py — the daily portfolio digest email.

What these pin, and why:

- The digest is the *only* thing that tells the owner something broke. Every failure
  mode here is "the email silently stops being useful", so the tests lean on the
  degrade-don't-crash paths rather than the happy path.
- The module is importable with zero side effects (imports + constants only),
  and all I/O sits behind five patchable module-level functions: fetch_alerts,
  fetch_mini_data, fetch_tasks, fetch_scheduled_jobs, send_email.
- Nothing here may touch the network, the Mac Mini, or launchd. Every test that
  reaches a subprocess patches it, so the file passes on ubuntu-latest where
  `launchctl` and `ssh` to the Mini do not exist.
"""

import io
from datetime import timedelta
import json
import urllib.error

import pytest

from scripts import alert_digest as ad


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """RETRY_BACKOFF_SECONDS is 3 and there are four retry loops of 3 attempts;
    unpatched, this file would take 20+ seconds."""
    monkeypatch.setattr(ad.time, "sleep", lambda _s: None)


def test_log_writes_one_stderr_line_and_no_file(monkeypatch, capsys):
    # launchd already captures stderr; a second copy in a log file was 6.6's
    # double log. AssertionError, not OSError: the old copy swallowed OSError.
    def no_file(*_a, **_k):
        raise AssertionError("log() must not write a file")
    monkeypatch.setattr("builtins.open", no_file)
    monkeypatch.setattr(ad.Path, "open", no_file)

    ad.log("hello")

    out, err = capsys.readouterr()
    assert out == ""
    assert err.count("\n") == 1 and err.rstrip().endswith(" hello")


_REAL_FETCH_CALENDAR = ad.fetch_calendar_events
DIGEST = ad.DigestIntegration("me@example", "Alerts <alerts@example>", ("com.pt.",), "mini.example")


@pytest.fixture(autouse=True)
def _digest_config(tmp_path, monkeypatch):
    """main() reads the [digest] section of the local config: give it DIGEST's values."""
    path = tmp_path / "digest-config.toml"
    path.write_text('[digest]\nto = "me@example"\nfrom = "Alerts <alerts@example>"\n'
                    'job_label_prefixes = ["com.pt."]\nmini_host = "mini.example"\n')
    monkeypatch.setenv("PT_CONFIG", str(path))
    return path


@pytest.fixture(autouse=True)
def _no_live_calendar(monkeypatch):
    """Keep every test off the live dashboard's calendar (no events by default)."""
    monkeypatch.setattr(ad, "fetch_calendar_events", lambda: [])


class _FakeResp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload, code=200):
        self._body = json.dumps(payload).encode("utf-8")
        self._code = code

    def read(self):
        return self._body

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code=500, body=b"quota exceeded"):
    return urllib.error.HTTPError(
        "https://api.resend.com/emails", code, "err", {}, io.BytesIO(body)
    )


def _stub_fetchers(monkeypatch, *, mini=None, tasks=None, jobs=None):
    """Cut every external dependency of main(): no HTTP, no ssh, no launchctl."""
    monkeypatch.setattr(ad, "fetch_mini_data", lambda *a: mini)
    monkeypatch.setattr(ad, "fetch_tasks", lambda: tasks)
    monkeypatch.setattr(ad, "fetch_scheduled_jobs", lambda *a: jobs)


# --- 1. RESEND_API_KEY guard ------------------------------------------------
# "Secrets come from Doppler. A missing secret must crash the app, not silently
# stub it." This is the one place the digest is allowed to hard-fail.

class TestApiKeyGuard:

    def test_send_email_raises_without_api_key(self, monkeypatch):
        monkeypatch.delenv("RESEND_API_KEY", raising=False)
        with pytest.raises(RuntimeError) as exc:
            ad.send_email("subject", "<p>body</p>", DIGEST)
        msg = str(exc.value)
        assert "RESEND_API_KEY" in msg
        assert "doppler run" in msg

    def test_guard_fires_before_any_network_call(self, monkeypatch):
        """No fallback send, no half-attempt: it raises before urlopen."""
        monkeypatch.delenv("RESEND_API_KEY", raising=False)

        def _boom(*a, **kw):  # pragma: no cover - must never run
            raise AssertionError("urlopen must not be reached without a key")

        monkeypatch.setattr(ad.urllib.request, "urlopen", _boom)
        with pytest.raises(RuntimeError):
            ad.send_email("subject", "<p>body</p>", DIGEST)


# --- 2. Retry logic ---------------------------------------------------------
# Four loops of identical shape, MAX_RETRIES = 3. Each is pinned for: recovers
# on attempt 2, gives up after *exactly* MAX_RETRIES attempts.

class TestFetchAlertsRetries:

    def test_missing_alerts_key_is_empty_not_crash(self, monkeypatch):
        monkeypatch.setattr(
            ad.urllib.request, "urlopen", lambda req, timeout=None: _FakeResp({})
        )
        assert ad.fetch_alerts() == []


class TestFetchTasksRetries:

    def test_unexpected_shape_is_retried_then_none(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return _FakeResp({"tasks": {"not": "a list"}})

        monkeypatch.setattr(ad.urllib.request, "urlopen", fake_urlopen)
        assert ad.fetch_tasks() is None
        assert len(calls) == ad.MAX_RETRIES


class TestFetchMiniRetries:
    """The Mini path shells out over SSH — always patched, never dialed."""

    def test_missing_scanner_short_circuits(self, monkeypatch, tmp_path):
        monkeypatch.setattr(ad, "MINI_SCAN_SCRIPT", tmp_path / "absent.py")

        def _boom(*a, **kw):  # pragma: no cover - must never run
            raise AssertionError("ssh must not be spawned without a scanner")

        monkeypatch.setattr(ad.subprocess, "run", _boom)
        assert ad.fetch_mini_data("mini.example", ()) is None

    def test_the_configured_host_and_prefixes_reach_ssh(self, monkeypatch, mini_scanner):
        calls = _sequence(monkeypatch, *_RUN, [_Proc(0, out=json.dumps(_MINI_OK))])
        assert ad.fetch_mini_data("mini.example", ("com.example.", "a b")) == _MINI_OK
        assert calls[0][0][-5:] == ["mini.example", "python3", "-", "com.example.", "'a b'"]

class TestSendEmailRetries:

    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("RESEND_API_KEY", "re_test_key")

    def test_http_error_body_is_surfaced(self, monkeypatch):
        """HTTPError is handled separately so Resend's reason reaches the log."""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            raise _http_error(422, b"domain not verified")

        monkeypatch.setattr(ad.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError) as exc:
            ad.send_email("subject", "<p>hi</p>", DIGEST)
        assert len(calls) == ad.MAX_RETRIES
        msg = str(exc.value)
        assert "HTTP 422" in msg
        assert "domain not verified" in msg

    def test_unexpected_success_code_is_a_failure(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            return _FakeResp({}, code=204)

        monkeypatch.setattr(ad.urllib.request, "urlopen", fake_urlopen)
        with pytest.raises(RuntimeError):
            ad.send_email("subject", "<p>hi</p>", DIGEST)
        assert len(calls) == ad.MAX_RETRIES


class _Proc:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


@pytest.fixture
def mini_scanner(monkeypatch, tmp_path):
    script = tmp_path / "mini_scan.py"
    script.write_text("print('{}')\n")
    monkeypatch.setattr(ad, "MINI_SCAN_SCRIPT", script)
    return script


def _sequence(monkeypatch, owner, name, behaviours):
    """Stub owner.name: each call plays the next behaviour (the last repeats);
    an exception instance is raised, anything else returned. Returns the call list."""
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        b = behaviours[min(len(calls), len(behaviours)) - 1]
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(owner, name, fake)
    return calls


def _send():
    return ad.send_email("subject", "<p>hi</p>", DIGEST)


def _mini():
    return ad.fetch_mini_data("mini.example", ())


_URLOPEN = (ad.urllib.request, "urlopen")
_RUN = (ad.subprocess, "run")
_MINI_OK = {"projects": [], "scanned_at": "now"}


@pytest.mark.parametrize(
    "stub, behaviours, call, expected",
    [
        pytest.param(
            _URLOPEN,
            [urllib.error.URLError("connection refused"),
             _FakeResp({"alerts": [{"project_id": "a"}]})],
            ad.fetch_alerts, [{"project_id": "a"}], id="fetch_alerts"),
        pytest.param(
            _URLOPEN,
            [urllib.error.URLError("nope"), _FakeResp({"tasks": [{"display_id": "1"}]})],
            ad.fetch_tasks, [{"display_id": "1"}], id="fetch_tasks"),
        pytest.param(
            _RUN,
            [_Proc(255, err="ssh: connect to host ... refused"),
             _Proc(0, out=json.dumps(_MINI_OK))],
            _mini, _MINI_OK, id="fetch_mini"),
        pytest.param(
            _URLOPEN,
            [OSError("transient socket error"), _FakeResp({"id": "abc"}, code=200)],
            _send, None, id="send_email"),
    ],
)
def test_retry_succeeds_on_second_attempt(
    monkeypatch, mini_scanner, stub, behaviours, call, expected
):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    calls = _sequence(monkeypatch, *stub, behaviours)
    assert call() == expected
    assert len(calls) == 2


@pytest.mark.parametrize(
    "stub, failure, call, raises, result",
    [
        pytest.param(
            _URLOPEN, urllib.error.URLError("connection refused"),
            ad.fetch_alerts, "unreachable", None, id="fetch_alerts"),
        # Unlike fetch_alerts, tasks degrade to None rather than raising —
        # a missing board must not take down the whole digest.
        pytest.param(
            _URLOPEN, urllib.error.URLError("nope"),
            ad.fetch_tasks, None, None, id="fetch_tasks"),
        pytest.param(
            _RUN, OSError("host down"), _mini, None, None, id="fetch_mini"),
        pytest.param(
            _URLOPEN, OSError("socket dead"), _send, "socket dead", None, id="send_email"),
    ],
)
def test_retry_gives_up_after_exactly_max_retries(
    monkeypatch, mini_scanner, stub, failure, call, raises, result
):
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    calls = _sequence(monkeypatch, *stub, [failure])
    if raises:
        with pytest.raises(RuntimeError) as exc:
            call()
        assert raises in str(exc.value)
    else:
        assert call() is result
    assert len(calls) == ad.MAX_RETRIES == 3


# --- 3. Ignore filter -------------------------------------------------------

class TestIgnoreFilterKeyAsymmetry:
    """The two machines filter on DIFFERENT keys, and that is load-bearing:
    main() matches alerts on ``project_id``; render_mini_section matches Mini
    projects on ``name`` (the Mini scan has no ids, only directory names).
    Anyone "unifying" these will break one machine's ignore list silently.
    """

    def test_macbook_filters_on_project_id_not_name(self, monkeypatch, _digest_config, capsys):
        _stub_fetchers(monkeypatch)
        _digest_config.write_text(_digest_config.read_text() + 'ignore = ["hidden"]\n')
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [
            {"project_id": "hidden", "project_name": "Shown Name",
             "severity": "critical", "message": "dropped by id"},
            {"project_id": "other", "project_name": "hidden",
             "severity": "critical", "message": "kept despite matching name"},
        ])
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "dropped by id" not in out
        assert "kept despite matching name" in out

    def test_the_configured_mini_ignore_list_reaches_the_mini_section(self, monkeypatch, _digest_config, capsys):
        mini = {"scanned_at": "2026-09-05T07:00:00Z",
                "projects": [{"name": "hidden", "days_since": 1}, {"name": "visible", "days_since": 1}]}
        _stub_fetchers(monkeypatch, mini=mini)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        _digest_config.write_text(_digest_config.read_text() + 'mini_ignore = ["hidden"]\n')
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "visible" in out and "hidden" not in out

    def test_mini_filters_on_name(self):
        mini = {
            "scanned_at": "2026-09-05T07:00:00Z",
            "projects": [
                {"name": "hidden", "days_since": 1},
                {"name": "visible", "days_since": 1},
            ],
        }
        html = ad.render_mini_section(mini, {"hidden"})
        assert "visible" in html
        assert "hidden" not in html
        assert "Scanned 1 projects" in html

    def test_mini_ignoring_by_id_would_not_work(self):
        """Sanity check on the asymmetry: a project_id-shaped entry misses."""
        mini = {"projects": [{"name": "visible", "days_since": 1}]}
        html = ad.render_mini_section(mini, {"some-project-id"})
        assert "visible" in html


# --- 4. Degraded-mode render ------------------------------------------------

class TestDegradedMode:

    def test_unreachable_dashboard_degrades_instead_of_going_silent(
        self, monkeypatch, tmp_path, capsys
    ):
        _stub_fetchers(monkeypatch)

        def _raise():
            raise RuntimeError("dashboard unreachable after 3 attempts: refused")

        monkeypatch.setattr(ad, "fetch_alerts", _raise)

        sent = []
        monkeypatch.setattr(ad, "send_email", lambda s, h, d: sent.append((s, h)))

        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "Subject: [Project Alerts] ⚠️ Digest degraded" in out
        assert "Could not read alerts on this machine" in out
        assert "dashboard unreachable after 3 attempts" in out
        # --dry-run must never send.
        assert sent == []

    def test_dry_run_never_sends_on_the_happy_path_either(
        self, monkeypatch, tmp_path, capsys
    ):
        _stub_fetchers(monkeypatch)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        sent = []
        monkeypatch.setattr(ad, "send_email", lambda s, h, d: sent.append((s, h)))

        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "Subject: [Project Alerts] ✅ All clear" in out
        assert sent == []

    def test_send_failure_returns_nonzero_without_raising(
        self, monkeypatch, tmp_path
    ):
        _stub_fetchers(monkeypatch)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])

        def _fail(subject, html, digest):
            raise RuntimeError("resend down")

        monkeypatch.setattr(ad, "send_email", _fail)
        assert ad.main([]) == 1

    def test_successful_send_returns_zero(self, monkeypatch, tmp_path):
        _stub_fetchers(monkeypatch)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [
            {"project_id": "p", "project_name": "P", "severity": "warning",
             "message": "m", "details": "d"},
        ])
        sent = []
        monkeypatch.setattr(ad, "send_email", lambda s, h, d: sent.append((s, h)))
        assert ad.main([]) == 0
        assert len(sent) == 1
        assert sent[0][0] == "[Project Alerts] 🟡 1 warning"


class TestSubjectLine:

    def test_all_clear(self):
        assert ad.build_subject([]) == "[Project Alerts] ✅ All clear"

    def test_counts_by_severity(self):
        alerts = [
            {"severity": "critical"}, {"severity": "warning"},
            {"severity": "warning"}, {"severity": "info"},
        ]
        assert ad.build_subject(alerts) == (
            "[Project Alerts] 🔴 1 critical, 🟡 2 warnings, 🔵 1 info"
        )


# --- 5. Escaping regressions (#6439) ----------------------------------------
# Every value below arrives from outside the digest: alert_detector passes
# through scanned project text, project names come from disk/DB, and the Mini
# error string is whatever came back over SSH.

XSS = '<script>alert("pwned")</script>'


def _assert_escaped(html: str) -> None:
    assert "<script>" not in html
    assert "</script>" not in html
    assert "&lt;script&gt;" in html


class TestHtmlEscaping:

    def test_esc_handles_the_three_text_node_chars(self):
        assert ad._esc("a & b < c > d") == "a &amp; b &lt; c &gt; d"
        assert ad._esc(42) == "42"

    @pytest.mark.parametrize("field", ["project_name", "message", "details"])
    def test_alert_row_field_is_escaped(self, field):
        alert = {"project_name": "p", "severity": "critical", "message": "m", "details": "d"}
        alert[field] = XSS
        _assert_escaped(ad._render_alert_rows([alert]))

    @pytest.mark.parametrize(
        "data, stale",
        [
            # The most attacker-adjacent value: an error returned over SSH.
            pytest.param({"error": XSS}, False, id="error_string"),
            pytest.param(
                {"scanned_at": XSS, "projects": [{"name": "p", "days_since": 1}]},
                False, id="scanned_at"),
            pytest.param(
                {"scanned_at": "now", "projects": [{"name": XSS, "days_since": 1}]},
                False, id="active_project_name"),
            pytest.param(
                {"scanned_at": "now",
                 "projects": [{"name": XSS, "days_since": ad.STALE_DAYS + 5}]},
                True, id="stale_project_name"),
        ],
    )
    def test_mini_field_is_escaped(self, data, stale):
        html = ad.render_mini_section(data, set())
        _assert_escaped(html)
        if stale:
            assert "Stale (60+ days)" in html

    def test_full_email_render_is_escaped_end_to_end(self):
        html = ad.render_html(
            macbook_alerts=[{"project_name": XSS, "severity": "critical",
                             "message": XSS, "details": XSS}],
            mini_data={"scanned_at": XSS,
                       "projects": [{"name": XSS, "days_since": 2}],
                       "jobs": [{"label": "com.pt." + XSS, "pid": None,
                                 "last_exit": 1}]},
            mini_ignore=set(),
            tasks=[{"display_id": "1", "title": XSS, "project_id": XSS,
                    "status": "In Progress", "updated_at": "2026-09-01"}],
            jobs=[{"label": "com.pt." + XSS, "pid": None, "last_exit": 1}],
            degraded_reason=None,
            sent_at="Friday, September 05 2026 · 7:00 AM",
        )
        _assert_escaped(html)


# --- Rendering odds and ends ------------------------------------------------

class TestCardsAndJobsRendering:

    def test_cards_section_notice_when_board_unreadable(self):
        html = ad.render_cards_section(None)
        assert "Could not read the board this run" in html

    def test_backlog_is_grouped_by_project_not_listed(self):
        tasks = [
            {"display_id": str(i), "title": f"card {i}", "project_id": "alpha",
             "status": "Backlog"} for i in range(5)
        ]
        html = ad.render_cards_section(tasks)
        assert "🗄️ Backlog (5)" in html
        assert "card 0" not in html
        assert "alpha" in html

    def test_jobs_notice_when_launchctl_unavailable(self):
        """None (e.g. Linux, no launchctl) renders a notice, not a crash."""
        html = ad.render_jobs_section(None, None)
        assert html.count("no job data this run") == 2

    def test_failed_job_is_flagged_and_sorted_first(self):
        html = ad._render_job_group([
            {"label": "com.pt.aaa_ok", "pid": None, "last_exit": 0},
            {"label": "com.pt.zzz_bad", "pid": None, "last_exit": 3},
        ], self.ATTENTION)
        assert "FAILED (exit 3)" in html
        assert html.index("zzz_bad") < html.index("aaa_ok")

    # --- #7755: documented non-failure exit codes render as ATTENTION, not FAILED ---

    SEO_LABEL = "com.example.seo-monitor"
    # What the config's [[digest.attention]] gives the renderer: (label, exit code) -> report.
    ATTENTION = {(SEO_LABEL, 2): "~/monitor/seo-monitor/report.md"}

    def test_documented_attention_exit_renders_attention_not_failed(self):
        """exit 2 for the seo job is listed as a documented non-failure state."""
        html = ad._render_job_group([
            {"label": self.SEO_LABEL, "pid": None, "last_exit": 2},
        ], self.ATTENTION)
        assert "FAILED" not in html
        assert "ATTENTION (exit 2)" in html
        # The report path is surfaced so the email is actionable, not just quiet.
        assert "seo-monitor/report.md" in html

    @pytest.mark.parametrize(
        "label, exit_code",
        [
            # exit 1 for the SAME job is not listed, so it stays a failure.
            pytest.param(SEO_LABEL, 1, id="same_job_undocumented_exit"),
            # exit 2 means nothing special for a job with no entry in the table —
            # never silently downgrade an undocumented code.
            pytest.param("com.example.some-other-job", 2, id="unlisted_job_same_exit"),
        ],
    )
    def test_undocumented_exit_is_failed(self, label, exit_code):
        html = ad._render_job_group([{"label": label, "pid": None, "last_exit": exit_code}], self.ATTENTION)
        assert f"FAILED (exit {exit_code})" in html
        assert "ATTENTION" not in html

    def test_attention_sorts_between_failed_and_ok(self):
        html = ad._render_job_group([
            {"label": "com.pt.aaa_ok", "pid": None, "last_exit": 0},
            {"label": self.SEO_LABEL, "pid": None, "last_exit": 2},
            {"label": "com.pt.zzz_bad", "pid": None, "last_exit": 3},
        ], self.ATTENTION)
        assert html.index("zzz_bad") < html.index("seo-monitor") < html.index("aaa_ok")

    def test_attention_job_does_not_change_subject_or_counts(self):
        """Scheduled-job state is rendered in its own section and must never
        leak into the dashboard-alert-derived subject/summary counts — an
        ATTENTION job is not a dashboard alert and must not be counted as one."""
        counts_before = ad._summary_counts([])
        subject = ad.build_subject([])
        html = ad.render_jobs_section(
            [{"label": self.SEO_LABEL, "pid": None, "last_exit": 2}], None, attention=self.ATTENTION
        )
        assert "ATTENTION" in html
        # Subject/counts are built from dashboard alerts only; an empty alerts
        # list stays "All clear" regardless of job state.
        assert subject == "[Project Alerts] ✅ All clear"
        assert counts_before == {"critical": 0, "warning": 0, "info": 0}


class TestDigestConfig:
    """The digest runs only from a [digest] section of the local config (#8677)."""

    @staticmethod
    def _boom(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("an unconfigured or broken digest must not fetch or send")

    @pytest.mark.parametrize("text, code", [("", 0), ("[digest]\nto = 1\n", 1)])
    def test_absent_is_off_and_broken_is_an_error(self, monkeypatch, tmp_path, capsys, text, code):
        path = tmp_path / "c.toml"
        path.write_text(text)
        monkeypatch.setenv("PT_CONFIG", str(path))
        for name in ("fetch_alerts", "fetch_mini_data", "fetch_tasks", "fetch_scheduled_jobs", "send_email"):
            monkeypatch.setattr(ad, name, self._boom)
        assert ad.main([]) == code
        err = capsys.readouterr().err
        assert str(path) in err and ("not configured" in err) is (code == 0)

    @pytest.mark.parametrize("host", ['"mini.example"', None])
    def test_the_mini_section_follows_mini_host(self, monkeypatch, tmp_path, capsys, host):
        path = tmp_path / "c.toml"
        path.write_text('[digest]\nto = "a@example"\nfrom = "b@example"\n'
                        + (f"mini_host = {host}\n" if host else ""))
        monkeypatch.setenv("PT_CONFIG", str(path))
        _stub_fetchers(monkeypatch)
        if not host:
            monkeypatch.setattr(ad, "fetch_mini_data", self._boom)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "To: a@example\nFrom: b@example" in out
        assert ("Mac Mini" in out) is bool(host)


class TestRunsUnderDoppler:
    """[digest] doppler_project/doppler_config: main() runs itself again under doppler run, once."""

    @pytest.fixture
    def doppler_config(self, tmp_path, monkeypatch):
        path = tmp_path / "c.toml"
        path.write_text('[digest]\nto = "a@example"\nfrom = "b@example"\n'
                        'doppler_project = "mail"\ndoppler_config = "prd"\n')
        monkeypatch.setenv("PT_CONFIG", str(path))
        # setenv, not delenv: main() writes the marker into os.environ, and only
        # a recorded variable is put back afterwards.
        monkeypatch.setenv("PT_DIGEST_UNDER_DOPPLER", "")

    def test_reruns_itself_under_the_configured_project(self, monkeypatch, doppler_config):
        seen = []

        def fake_exec(file, args):
            seen.append(args)
            raise SystemExit(0)  # exec never returns

        monkeypatch.setattr(ad.os, "execvp", fake_exec)
        with pytest.raises(SystemExit):
            ad.main(["--dry-run"])
        assert seen[0][:7] == ["doppler", "run", "--project", "mail", "--config", "prd", "--"]
        assert seen[0][-2:] == [str(ad.Path(ad.__file__).resolve()), "--dry-run"]
        assert ad.os.environ["PT_DIGEST_UNDER_DOPPLER"] == "1"

    def test_under_doppler_it_runs_instead_of_exec_again(self, monkeypatch, tmp_path, doppler_config, capsys):
        monkeypatch.setenv("PT_DIGEST_UNDER_DOPPLER", "1")
        monkeypatch.setattr(ad.os, "execvp", lambda *a: pytest.fail("must not exec twice"))
        _stub_fetchers(monkeypatch)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        assert ad.main(["--dry-run"]) == 0
        assert "To: a@example" in capsys.readouterr().out

    def test_no_doppler_binary_is_an_error_and_sends_nothing(self, monkeypatch, tmp_path, doppler_config):
        def missing(*_a):
            raise FileNotFoundError("doppler")

        monkeypatch.setattr(ad.os, "execvp", missing)
        _stub_fetchers(monkeypatch)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        sent = []
        monkeypatch.setattr(ad, "send_email", lambda s, h, d: sent.append(s))
        assert ad.main([]) == 1 and sent == []

    def test_attention_rules_reach_the_email(self, monkeypatch, tmp_path, capsys):
        path = tmp_path / "c.toml"
        path.write_text('[digest]\nto = "a"\nfrom = "b"\n[[digest.attention]]\n'
                        'label = "com.pt.monitor"\nexit_code = 2\nreport = "~/monitor/report.md"\n')
        monkeypatch.setenv("PT_CONFIG", str(path))
        _stub_fetchers(monkeypatch, jobs=[{"label": "com.pt.monitor", "pid": None, "last_exit": 2}])
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        assert ad.main(["--dry-run"]) == 0
        assert "ATTENTION (exit 2) — see ~/monitor/report.md" in capsys.readouterr().out


class TestFetchScheduledJobs:
    """launchctl does not exist on Linux CI, so the subprocess is always patched."""

    class _Proc:  # launchctl_list reads bytes
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out.encode(), err.encode()

    def test_parses_and_filters_to_our_labels(self, monkeypatch):
        out = (
            "PID\tStatus\tLabel\n"
            "-\t0\tcom.apple.something\n"
            "1234\t0\tcom.example.digest\n"
            "-\t1\tcom.pt.dashboard\n"
            "-\t-\tcom.pt.neverrun\n"
        )
        monkeypatch.setattr(
            ad.subprocess, "run", lambda cmd, **kw: self._Proc(0, out=out)
        )
        jobs = ad.fetch_scheduled_jobs(("com.example.", "com.pt."))
        assert [j["label"] for j in jobs] == [
            "com.example.digest", "com.pt.dashboard", "com.pt.neverrun"
        ]
        assert jobs[0]["pid"] == "1234"
        assert jobs[1]["last_exit"] == 1
        assert jobs[2]["pid"] is None and jobs[2]["last_exit"] is None

    def test_malformed_status_column_does_not_crash(self, monkeypatch):
        out = "PID\tStatus\tLabel\n-\tWAT\tcom.pt.weird\n"
        monkeypatch.setattr(
            ad.subprocess, "run", lambda cmd, **kw: self._Proc(0, out=out)
        )
        jobs = ad.fetch_scheduled_jobs(("com.pt.",))
        assert jobs == [{"label": "com.pt.weird", "pid": None, "last_exit": None}]

    def test_nonzero_exit_degrades_to_none(self, monkeypatch):
        monkeypatch.setattr(
            ad.subprocess, "run", lambda cmd, **kw: self._Proc(1, err="boom")
        )
        assert ad.fetch_scheduled_jobs(("com.pt.",)) is None

    def test_missing_launchctl_degrades_to_none(self, monkeypatch):
        def _missing(cmd, **kw):
            raise FileNotFoundError("launchctl")

        monkeypatch.setattr(ad.subprocess, "run", _missing)
        assert ad.fetch_scheduled_jobs(("com.pt.",)) is None


# --- escaping helpers (#6881) ----------------------------------------------


def test_esc_handles_text_node_characters():
    assert ad._esc('<script>&</script>') == (
        "&lt;script&gt;&amp;&lt;/script&gt;"
    )


def test_esc_attr_also_neutralises_quotes():
    """A quote inside an attribute value closes it early; _esc alone doesn't
    stop that, which is the whole reason _esc_attr exists."""
    hostile = '" onmouseover=alert(1) x="'
    assert '"' in ad._esc(hostile), "_esc is text-node-only by design"
    escaped = ad._esc_attr(hostile)
    assert '"' not in escaped
    assert "&quot;" in escaped
    # single quotes too
    assert "'" not in ad._esc_attr("it's")
    assert "&#x27;" in ad._esc_attr("it's")


# --- Deadlines (#8137) -------------------------------------------------------
# The calendar's only route to the recipient: due-soon and recently missed events.

def _event(id_, event_date, title="Thing due", status="active", **extra):
    return {"id": id_, "title": title, "event_date": event_date, "status": status, **extra}


class TestDeadlines:
    TODAY = ad.date(2026, 10, 8)

    def test_window_bounds_status_and_order(self):
        events = [
            _event(1, "2026-10-22", "in 14 days"),         # edge: shown
            _event(2, "2026-10-23", "in 15 days"),         # outside
            _event(3, "2026-09-08", "missed 30 days ago"), # edge: shown
            _event(4, "2026-09-07", "missed 31 days ago"), # stale: hidden
            _event(5, "2026-10-15", "done already", status="done"),
            _event(6, "2026-10-08", "today"),
            _event(7, "2026-10-15T00:00:00", "tax return"),
        ]
        got = ad.upcoming_deadlines(events, self.TODAY)
        assert [(e["id"], e["days"]) for e in got] == [(3, -30), (6, 0), (7, 7), (1, 14)]

    def test_unreadable_date_is_shown_not_dropped(self, monkeypatch):
        lines = []
        monkeypatch.setattr(ad, "log", lines.append)
        rows = ad.upcoming_deadlines(
            [_event(1, "2026-10-15", "fine"), _event(9, "15/10/2026", "Tax return"),
             _event(8, None, "No date at all")],
            self.TODAY,
        )
        assert [(e["id"], e["days"]) for e in rows] == [(9, None), (8, None), (1, 7)]
        assert rows[0]["problem"] == "date unreadable: '15/10/2026'"
        assert rows[1]["problem"] == "date unreadable: None"
        assert any("unreadable date" in line for line in lines)
        html = ad.render_deadlines_section(rows)
        assert "date unreadable: &#x27;15/10/2026&#x27;" in html or "date unreadable: '15/10/2026'" in html
        assert "Tax return" in html and "No date at all" in html

    def test_unknown_status_is_shown_finished_ones_are_not(self):
        rows = ad.upcoming_deadlines(
            [_event(1, "2026-10-15", "done one", status="done"),
             _event(2, "2026-10-15", "cancelled one", status="cancelled"),
             _event(3, "2026-10-15", "odd one", status="Active "),
             _event(4, "2026-10-15", "no status", status=None)],
            self.TODAY,
        )
        assert [(e["id"], e["problem"]) for e in rows] == [
            (3, "status unreadable: 'Active '"), (4, "status unreadable: None")]

    def test_an_unreadable_event_alone_is_never_all_clear(self, monkeypatch, capsys):
        monkeypatch.setattr(ad, "fetch_calendar_events", lambda: [_event(9, "Oct 15", "Tax return")])
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        _stub_fetchers(monkeypatch, tasks=[], jobs=[])
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        subject = out.splitlines()[0]
        assert "All clear" not in subject and "📅 1 deadline due or missed" in subject
        assert "date unreadable" in out and "Tax return" in out

    def test_section_is_absent_on_empty_days(self):
        assert ad.render_deadlines_section([]) == ""
        html = ad.render_html([], None, set(), [], [], None, "now", [])
        assert "Deadlines" not in html

    def test_section_lists_due_and_missed_with_escaping(self):
        rows = ad.upcoming_deadlines(
            [_event(1, "2026-10-15", "<b>Extended federal return</b>", project_id="tax-office"),
             _event(2, "2026-09-15", "Q3 estimated tax")],
            self.TODAY,
        )
        html = ad.render_deadlines_section(rows)
        assert "in 7 days" in html and "missed 23 days ago" in html
        assert "&lt;b&gt;Extended federal return&lt;/b&gt;" in html and "<b>Extended" not in html
        assert "tax-office" in html
        assert html.index("missed 23 days ago") < html.index("in 7 days")

    def test_unreadable_calendar_is_shown_not_hidden(self):
        assert "Could not read the calendar" in ad.render_deadlines_section(None)
        assert "⚠️ calendar unreadable" in ad.build_subject([], None)

    def test_subject_names_deadlines_within_a_week_only(self):
        week = ad.upcoming_deadlines([_event(1, "2026-10-15")], self.TODAY)
        later = ad.upcoming_deadlines([_event(2, "2026-10-20")], self.TODAY)
        assert ad.build_subject([], week) == "[Project Alerts] 📅 1 deadline due or missed"
        # Listed in the body, so never "All clear", but not flagged as urgent either.
        assert ad.build_subject([], later) == "[Project Alerts] 📅 1 upcoming deadline"
        assert ad.build_subject([], []) == "[Project Alerts] ✅ All clear"
        assert ad.build_subject([]) == "[Project Alerts] ✅ All clear"

    def test_main_dry_run_shows_the_tax_deadline(self, monkeypatch, capsys):
        today = ad.date.today()
        due = (today + timedelta(days=6)).isoformat()
        monkeypatch.setattr(ad, "fetch_calendar_events", lambda: [
            _event(9, due, "Extended federal tax return due (2025)", project_id="tax-office"),
        ])
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        _stub_fetchers(monkeypatch, tasks=[], jobs=[])
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "📅 1 deadline due or missed" in out.splitlines()[0]
        assert "📅 Deadlines" in out and "Extended federal tax return due (2025)" in out
        assert "in 6 days" in out

    def test_main_with_calendar_down_says_so(self, monkeypatch, capsys):
        monkeypatch.setattr(ad, "fetch_calendar_events", lambda: None)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        _stub_fetchers(monkeypatch, tasks=[], jobs=[])
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "calendar unreadable" in out.splitlines()[0]
        assert "Could not read the calendar" in out

    def test_fetch_reads_events_and_fails_soft(self, monkeypatch):
        monkeypatch.setattr(ad, "fetch_calendar_events", _REAL_FETCH_CALENDAR)
        monkeypatch.setattr(ad.urllib.request, "urlopen",
                            lambda req, timeout=10: _FakeResp({"events": [_event(1, "2026-10-15")], "total": 1}))
        assert [e["id"] for e in ad.fetch_calendar_events()] == [1]
        def down(req, timeout=10):
            raise ad.urllib.error.URLError("refused")
        monkeypatch.setattr(ad.urllib.request, "urlopen", down)
        assert ad.fetch_calendar_events() is None


    def test_non_object_event_is_a_read_failure(self, monkeypatch):
        monkeypatch.setattr(ad, "fetch_calendar_events", _REAL_FETCH_CALENDAR)
        monkeypatch.setattr(ad.urllib.request, "urlopen",
                            lambda req, timeout=10: _FakeResp({"events": [_event(1, "2026-10-15"), "junk"]}))
        assert ad.fetch_calendar_events() is None

    def test_unparseable_events_still_send_the_digest_with_a_warning(self, monkeypatch, capsys):
        monkeypatch.setattr(ad, "fetch_calendar_events", lambda: [_event(1, "2026-10-15")])
        def boom(events, today):
            raise TypeError("bad event")
        monkeypatch.setattr(ad, "upcoming_deadlines", boom)
        monkeypatch.setattr(ad, "fetch_alerts", lambda: [])
        _stub_fetchers(monkeypatch, tasks=[], jobs=[])
        assert ad.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "calendar unreadable" in out.splitlines()[0]
        assert "Could not read the calendar" in out

    def test_mixed_event_time_types_sort_without_crashing(self):
        rows = ad.upcoming_deadlines(
            [_event(1, "2026-10-15", "a", event_time=900), _event(2, "2026-10-15", "b"),
             _event(3, "2026-10-15", "c", event_time="08:00")],
            self.TODAY,
        )
        assert [e["id"] for e in rows] == [2, 3, 1]
        assert "900" in ad.render_deadlines_section(rows)


    def test_trailing_junk_is_unreadable_but_a_full_datetime_is_fine(self):
        rows = ad.upcoming_deadlines(
            [_event(1, "2026-10-15garbage", "junk"), _event(2, "2026-10-15T09:30:00", "datetime"),
             _event(3, "x" * 500, "huge")],
            self.TODAY,
        )
        by_id = {e["id"]: e for e in rows}
        assert by_id[1]["problem"] == "date unreadable: '2026-10-15garbage'"
        assert by_id[2]["days"] == 7 and "problem" not in by_id[2]
        assert len(by_id[3]["problem"]) <= len("date unreadable: ") + 80
