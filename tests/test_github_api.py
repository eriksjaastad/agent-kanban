"""Tests for GitHub API integration in the dashboard.

Covers:
- _gh_json: subprocess execution, error handling, timeouts
- _get_tracked_repo_names: project filtering, github-repos expansion
- _fetch_github_data: data assembly and structure
- /api/github endpoint: caching behavior, response structure
"""

import sys
import os
import asyncio
import subprocess
import warnings
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def as_gh_call(fn):
    """Adapt a data-returning mock to _gh_call's (data, failure_kind) contract.

    _fetch_github_data calls _gh_call, which reports WHY a call failed so a
    missing repo can be told apart from a broken fetch. Mocks stay written in
    terms of returned data; returning None means GH_ERROR, and a mock can return an
    explicit (data, kind) tuple when it needs to simulate GH_NOT_FOUND.
    """
    from dashboard.routes.github import GH_OK, GH_ERROR

    def wrapper(args, timeout=30, **_):
        result = fn(args, timeout)
        if isinstance(result, tuple):
            return result
        return (result, GH_OK if result is not None else GH_ERROR)

    return wrapper


class TestGhJson:
    """Tests for _gh_json — runs gh CLI commands and returns parsed JSON."""

    def test_successful_command(self):
        """_gh_json returns parsed JSON on success."""
        from dashboard.routes.github import _gh_json
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"login": "testuser"}'

        with patch("dashboard.routes.github.subprocess.run", return_value=mock_result) as mock_run:
            result = _gh_json(["api", "/user"])

        assert result == {"login": "testuser"}
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert call_args[0][0][-2:] == ["api", "/user"]
        assert call_args[1]["capture_output"] is True
        assert call_args[1]["text"] is True
        assert call_args[1]["timeout"] == 30

    def test_custom_timeout(self):
        """_gh_json passes custom timeout to subprocess."""
        from dashboard.routes.github import _gh_json
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "[]"

        with patch("dashboard.routes.github.subprocess.run", return_value=mock_result) as mock_run:
            _gh_json(["pr", "list"], timeout=15)

        assert mock_run.call_args[1]["timeout"] == 15

    @pytest.mark.parametrize(
        "run_kwargs",
        [
            pytest.param(  # gh exits non-zero
                {"return_value": MagicMock(returncode=1, stderr="not authenticated")},
                id="failed_command",
            ),
            pytest.param(  # subprocess timeout
                {"side_effect": subprocess.TimeoutExpired(cmd="gh", timeout=30)},
                id="timeout",
            ),
            pytest.param(  # stdout is not valid JSON
                {"return_value": MagicMock(returncode=0, stdout="This is not JSON at all")},
                id="non_json_output",
            ),
            pytest.param(  # gh CLI is not installed
                {"side_effect": FileNotFoundError()},
                id="missing_gh_binary",
            ),
            pytest.param(  # stdout is empty/whitespace
                {"return_value": MagicMock(returncode=0, stdout="   ")},
                id="empty_stdout",
            ),
        ],
    )
    def test_failure_modes_return_none(self, run_kwargs):
        """_gh_json returns None on a non-zero exit, timeout, non-JSON or empty
        stdout, and when the gh binary is missing."""
        from dashboard.routes.github import _gh_json

        with patch("dashboard.routes.github.subprocess.run", **run_kwargs):
            result = _gh_json(["api", "/user"])

        assert result is None

    def test_returns_list(self):
        """_gh_json can return a list, not just a dict."""
        from dashboard.routes.github import _gh_json
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '[{"name": "repo1"}, {"name": "repo2"}]'

        with patch("dashboard.routes.github.subprocess.run", return_value=mock_result):
            result = _gh_json(["pr", "list", "--json", "name"])

        assert isinstance(result, list)
        assert len(result) == 2


class TestGetTrackedRepoNames:
    """Tests for _get_tracked_repo_names — filters projects from kanban DB."""

    def test_returns_sorted_project_names(self):
        """Returns sorted list of project IDs, excluding internal prefixes."""
        from dashboard.routes.github import _get_tracked_repo_names

        mock_projects = [
            {"id": "project-tracker"},
            {"id": "trading-desk"},
            {"id": "ai-journey"},
        ]

        with patch("dashboard.routes.github.DatabaseManager") as MockDB:
            mock_db = MagicMock()
            mock_db.get_all_projects.return_value = mock_projects
            MockDB.return_value = mock_db

            result = _get_tracked_repo_names()

        assert result == ["ai-journey", "project-tracker", "trading-desk"]

    def test_filters_underscore_prefixed_projects(self):
        """Projects starting with _ or __ are excluded."""
        from dashboard.routes.github import _get_tracked_repo_names

        mock_projects = [
            {"id": "_toolbox"},
            {"id": "__knowledge"},
            {"id": "_collaboration"},
            {"id": "real-project"},
        ]

        with patch("dashboard.routes.github.DatabaseManager") as MockDB:
            mock_db = MagicMock()
            mock_db.get_all_projects.return_value = mock_projects
            MockDB.return_value = mock_db

            result = _get_tracked_repo_names()

        assert result == ["real-project"]
        assert "_toolbox" not in result
        assert "__knowledge" not in result

    def test_github_repos_expands_children(self, tmp_path):
        """github-repos project is replaced by its child directories."""
        from dashboard.routes.github import _get_tracked_repo_names

        # Create mock github-repos directory structure
        github_repos = tmp_path / "github-repos"
        github_repos.mkdir()
        (github_repos / "repo-alpha").mkdir()
        (github_repos / "repo-beta").mkdir()
        (github_repos / ".hidden").mkdir()
        (github_repos / "00_Index").mkdir()

        mock_projects = [
            {"id": "github-repos"},
            {"id": "other-project"},
        ]

        with patch("dashboard.routes.github.DatabaseManager") as MockDB:
            mock_db = MagicMock()
            mock_db.get_all_projects.return_value = mock_projects
            MockDB.return_value = mock_db

            with patch.dict(os.environ, {"PROJECTS_ROOT": str(tmp_path)}):
                result = _get_tracked_repo_names()

        assert "github-repos" not in result
        assert "repo-alpha" in result
        assert "repo-beta" in result
        assert ".hidden" not in result
        assert "00_Index" not in result
        assert "other-project" in result

    def test_github_repos_missing_dir(self, tmp_path):
        """If github-repos directory doesn't exist, it's just skipped."""
        from dashboard.routes.github import _get_tracked_repo_names

        mock_projects = [
            {"id": "github-repos"},
            {"id": "real-project"},
        ]

        with patch("dashboard.routes.github.DatabaseManager") as MockDB:
            mock_db = MagicMock()
            mock_db.get_all_projects.return_value = mock_projects
            MockDB.return_value = mock_db

            with patch.dict(os.environ, {"PROJECTS_ROOT": str(tmp_path)}):
                result = _get_tracked_repo_names()

        assert result == ["real-project"]

    def test_deduplication(self):
        """Duplicate project IDs are deduplicated."""
        from dashboard.routes.github import _get_tracked_repo_names

        mock_projects = [
            {"id": "project-a"},
            {"id": "project-a"},
            {"id": "project-b"},
        ]

        with patch("dashboard.routes.github.DatabaseManager") as MockDB:
            mock_db = MagicMock()
            mock_db.get_all_projects.return_value = mock_projects
            MockDB.return_value = mock_db

            result = _get_tracked_repo_names()

        assert result == ["project-a", "project-b"]

    def test_database_error_propagates(self):
        """A DB failure raises instead of reading as "no tracked projects" (#6900)."""
        import pytest
        from dashboard.routes.github import _get_tracked_repo_names

        with patch("dashboard.routes.github.DatabaseManager", side_effect=Exception("DB connection failed")):
            with pytest.raises(Exception, match="DB connection failed"):
                _get_tracked_repo_names()


class TestFetchGithubData:
    """Tests for _fetch_github_data — main data assembly function."""

    def test_returns_error_when_user_api_fails(self):
        """Returns error dict when gh api /user fails."""
        from dashboard.routes.github import _fetch_github_data

        with patch("dashboard.routes.github._gh_json", return_value=None):
            result = _fetch_github_data()

        assert "error" in result
        assert result["cached"] is False

    def test_returns_error_when_no_tracked_projects(self):
        """Returns error dict when no tracked projects found."""
        from dashboard.routes.github import _fetch_github_data

        def mock_gh_json(args, timeout=30):
            if args == ["api", "/user"]:
                return {"login": "testuser"}
            return None

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(mock_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names", return_value=[]):
                result = _fetch_github_data()

        assert "error" in result

    def test_successful_fetch_has_required_keys(self):
        """Successful fetch returns all expected top-level keys."""
        from dashboard.routes.github import _fetch_github_data

        user_data = {
            "login": "testuser",
            "name": "Test User",
            "avatar_url": "https://example.com/avatar.png",
            "public_repos": 10,
            "total_private_repos": 5,
            "followers": 100,
            "following": 50,
        }

        repo_data = {
            "name": "my-repo",
            "url": "https://github.com/testuser/my-repo",
            "pushedAt": "2020-01-01T00:00:00Z",
            "defaultBranchRef": {"name": "main"},
            "isPrivate": False,
            "description": "A test repo",
            "stargazerCount": 5,
            "forkCount": 1,
            "isArchived": False,
        }

        def mock_gh_json(args, timeout=30):
            if args == ["api", "/user"]:
                return user_data
            if args[0] == "repo" and args[1] == "view":
                return repo_data
            # Return empty lists/dicts for PRs, commits, runs, branches
            if args[0] == "pr":
                return []
            if "actions/runs" in str(args):
                return {"workflow_runs": []}
            if "branches" in str(args):
                return []
            if "commits" in str(args):
                return []
            return None

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(mock_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names", return_value=["my-repo"]):
                result = _fetch_github_data()

        assert "error" not in result
        required_keys = [
            "user", "repos", "tracked_projects", "open_pull_requests",
            "recent_commits", "workflow_runs", "branches", "summary",
            "fetched_at", "cached",
        ]
        for key in required_keys:
            assert key in result, f"Missing key: {key}"

        assert result["cached"] is False
        assert result["user"]["login"] == "testuser"
        assert len(result["repos"]) == 1
        assert result["summary"]["total_repos"] == 1

    def test_summary_counts_are_correct(self):
        """Summary section has correct computed counts."""
        from dashboard.routes.github import _fetch_github_data

        tracked = ["active-repo", "archived-repo", "missing-repo"]

        def selective_gh_json(args, timeout=30):
            from dashboard.routes.github import GH_NOT_FOUND
            if args == ["api", "/user"]:
                return {"login": "testuser"}
            if args[0] == "repo" and args[1] == "view":
                name = args[2].split("/")[1]
                if name == "missing-repo":
                    # Genuinely absent from GitHub, not a failed fetch — the
                    # two are counted separately now.
                    return (None, GH_NOT_FOUND)
                return {
                    "name": name,
                    "isArchived": name == "archived-repo",
                    "pushedAt": "2020-01-01T00:00:00Z",
                    "defaultBranchRef": {"name": "main"},
                }
            return []

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(selective_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names", return_value=tracked):
                result = _fetch_github_data()

        assert result["summary"]["tracked_projects"] == 3
        assert result["summary"]["repos_found_on_github"] == 2
        assert result["summary"]["repos_not_on_github"] == 1
        assert result["summary"]["archived_repos"] == 1
        # A repo that is absent from GitHub is not a fetch failure.
        assert result["summary"]["fetch_errors"] == 0


class TestApiGithubEndpoint:
    def test_endpoint_delegates_without_waiting_for_collection(self):
        from dashboard.routes.github import api_github
        pending = {"cached": False, "refreshing": True}
        with patch("dashboard.routes.github._github_cache") as cache:
            cache.read.return_value = pending
            assert asyncio.run(api_github()) == pending
            cache.read.assert_called_once()


class TestPrListFields:
    """Regression guard for #6749 — a bad --json field empties the PR panel.

    `gh pr list` rejects the entire call if any requested field is unknown, so
    a single typo turns every PR fetch into a silent no-op. These tests fail
    loudly instead.
    """

    def test_requested_fields_are_all_valid(self):
        """Every field in PR_LIST_FIELDS is accepted by `gh pr list --json`."""
        from dashboard.routes.github import PR_LIST_FIELDS, GH_PR_LIST_VALID_FIELDS

        invalid = set(PR_LIST_FIELDS) - GH_PR_LIST_VALID_FIELDS
        assert not invalid, (
            f"Invalid `gh pr list --json` field(s): {sorted(invalid)}. "
            "gh rejects the whole call, so every PR fetch would return nothing."
        )

    def test_fields_cover_what_the_frontend_reads(self):
        """Fields the dashboard UI renders are actually requested."""
        from dashboard.routes.github import PR_LIST_FIELDS

        for field in ("title", "number", "url", "author", "createdAt",
                      "headRefName", "isDraft", "statusCheckRollup"):
            assert field in PR_LIST_FIELDS

    def test_gh_invoked_with_valid_fields(self):
        """The actual `gh pr list` call sends only valid fields."""
        from dashboard.routes.github import _fetch_github_data, GH_PR_LIST_VALID_FIELDS

        seen = []

        def mock_gh_json(args, timeout=30):
            if args == ["api", "/user"]:
                return {"login": "testuser"}
            if args[0] == "repo" and args[1] == "view":
                return {
                    "name": args[2].split("/")[1],
                    "isArchived": False,
                    "pushedAt": "2020-01-01T00:00:00Z",
                    "defaultBranchRef": {"name": "main"},
                }
            if args[0] == "pr":
                seen.append(args[args.index("--json") + 1])
                return []
            return []

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(mock_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names", return_value=["my-repo"]):
                _fetch_github_data()

        assert seen, "no `gh pr list` call was made"
        for spec in seen:
            for field in spec.split(","):
                assert field in GH_PR_LIST_VALID_FIELDS, f"invalid field: {field}"


class TestPrRepositoryAttribution:
    """PRs must carry a repository name — `gh pr list` does not supply one."""

    @staticmethod
    def _fetch(tracked, prs_by_repo):
        from dashboard.routes.github import _fetch_github_data

        def mock_gh_json(args, timeout=30):
            if args == ["api", "/user"]:
                return {"login": "testuser"}
            if args[0] == "repo" and args[1] == "view":
                return {
                    "name": args[2].split("/")[1],
                    "isArchived": False,
                    "pushedAt": "2020-01-01T00:00:00Z",
                    "defaultBranchRef": {"name": "main"},
                }
            if args[0] == "pr":
                repo = args[args.index("--repo") + 1].split("/")[1]
                return prs_by_repo.get(repo, [])
            return []

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(mock_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names", return_value=tracked):
                return _fetch_github_data()

    def test_repository_name_attached_to_each_pr(self):
        """Each PR gets repository.name set from the repo it was fetched for."""
        result = self._fetch(
            ["repo-a", "repo-b"],
            {
                "repo-a": [{"number": 1, "title": "A", "headRefName": "feat/a"}],
                "repo-b": [{"number": 2, "title": "B", "headRefName": "feat/b"}],
            },
        )

        by_number = {pr["number"]: pr for pr in result["open_pull_requests"]}
        assert by_number[1]["repository"] == {"name": "repo-a"}
        assert by_number[2]["repository"] == {"name": "repo-b"}
        assert result["summary"]["open_prs"] == 2

    def test_failing_ci_correlates_pr_to_repo(self):
        """summary.failing_ci matches a failed run to a PR in the same repo."""
        from dashboard.routes.github import _fetch_github_data

        def mock_gh_json(args, timeout=30):
            if args == ["api", "/user"]:
                return {"login": "testuser"}
            if args[0] == "repo" and args[1] == "view":
                return {
                    "name": args[2].split("/")[1],
                    "isArchived": False,
                    "pushedAt": "2020-01-01T00:00:00Z",
                    "defaultBranchRef": {"name": "main"},
                }
            if args[0] == "pr":
                return [{"number": 1, "title": "A", "headRefName": "feat/a"}]
            if "actions/runs" in str(args):
                return {"workflow_runs": [{
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "feat/a",
                    "created_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://example.com/run/1",
                }]}
            return []

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(mock_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names", return_value=["repo-a"]):
                result = _fetch_github_data()

        assert result["summary"]["failing_ci"] == 1


class TestGhFieldListDrift:
    """GH_PR_LIST_VALID_FIELDS is a snapshot of gh's field list — catch drift.

    Skipped when gh is unavailable so CI without gh stays green. When gh IS
    present this asserts our snapshot still matches reality, in both
    directions: a field gh dropped (our request would break) and a field gh
    added (our snapshot would wrongly reject a valid new field).
    """

    @staticmethod
    def _live_fields():
        import shutil
        gh = shutil.which("gh")
        if not gh:
            return None
        # gh lists the valid fields in its error message for a bogus field.
        proc = subprocess.run(
            [gh, "pr", "list", "--repo", "cli/cli", "--json", "__bogus__", "--limit", "1"],
            capture_output=True, text=True, timeout=30,
        )
        text = proc.stdout + proc.stderr
        if "Available fields:" not in text:
            return None
        tail = text.split("Available fields:", 1)[1]
        return {line.strip() for line in tail.splitlines() if line.strip()}

    def test_requested_fields_still_accepted_by_live_gh(self):
        """The fields we actually request are still valid in the installed gh.

        Only the REMOVAL direction can break us: if gh drops a field we ask
        for, every PR fetch dies. A gh release that ADDS fields leaves our
        request a valid subset, so it must not turn CI red — it is reported as
        a warning instead.
        """
        import pytest
        from dashboard.routes.github import PR_LIST_FIELDS, GH_PR_LIST_VALID_FIELDS

        live = self._live_fields()
        if live is None:
            pytest.skip("gh CLI unavailable or output format changed")

        missing = set(PR_LIST_FIELDS) - live
        assert not missing, (
            f"The installed gh no longer accepts: {sorted(missing)}. "
            "Every PR fetch would fail. Update PR_LIST_FIELDS in dashboard/app.py."
        )

        stale = set(GH_PR_LIST_VALID_FIELDS) - live
        assert not stale, (
            f"gh removed field(s) still listed in GH_PR_LIST_VALID_FIELDS: {sorted(stale)}. "
            "The snapshot would wrongly accept them. Update dashboard/app.py."
        )

        added = live - set(GH_PR_LIST_VALID_FIELDS)
        if added:
            warnings.warn(
                f"gh added `pr list --json` field(s) not in GH_PR_LIST_VALID_FIELDS: "
                f"{sorted(added)}. Nothing is broken — our request is still a valid "
                "subset — but the snapshot in dashboard/app.py is behind.",
                UserWarning,
                stacklevel=2,
            )


class TestPrPartialFailure:
    """A failure in one repo must not lose PRs from repos that succeeded."""

    def test_partial_failure_keeps_good_repos(self):
        """One repo failing must not discard PRs from repos that succeeded."""
        from dashboard.routes.github import _fetch_github_data

        def mock_gh_json(args, timeout=30):
            if args == ["api", "/user"]:
                return {"login": "testuser"}
            if args[0] == "repo" and args[1] == "view":
                return {
                    "name": args[2].split("/")[1],
                    "isArchived": False,
                    "pushedAt": "2020-01-01T00:00:00Z",
                    "defaultBranchRef": {"name": "main"},
                }
            if args[0] == "pr":
                repo = args[args.index("--repo") + 1].split("/")[1]
                if repo == "broken-repo":
                    return None          # fetch failed
                if repo == "quiet-repo":
                    return []            # genuinely no open PRs
                return [{"number": 7, "title": "Good", "headRefName": "feat/g"}]
            return []

        with patch("dashboard.routes.github._gh_call", side_effect=as_gh_call(mock_gh_json)):
            with patch("dashboard.routes.github._get_tracked_repo_names",
                       return_value=["good-repo", "broken-repo", "quiet-repo"]):
                result = _fetch_github_data()

        # The working repo's PR survives its neighbour's failure.
        assert len(result["open_pull_requests"]) == 1
        assert result["open_pull_requests"][0]["repository"] == {"name": "good-repo"}
        assert result["summary"]["open_prs"] == 1

        # Only the genuine failure is reported — the empty repo is not an error.
        assert result["summary"]["fetch_errors"] == 1
        assert any("broken-repo" in e for e in result["fetch_errors"])
        assert not any("quiet-repo" in e for e in result["fetch_errors"])


class TestGhCallClassification:
    """_gh_call must tell a missing target apart from a broken fetch."""

    @staticmethod
    def _run(returncode, stdout="", stderr=""):
        from dashboard.routes.github import _gh_call
        mock_result = MagicMock()
        mock_result.returncode = returncode
        mock_result.stdout = stdout
        mock_result.stderr = stderr
        with patch("dashboard.routes.github.subprocess.run", return_value=mock_result):
            return _gh_call(["repo", "view", "owner/thing", "--json", "name"])

    def test_success_returns_data_and_ok(self):
        from dashboard.routes.github import GH_OK
        assert self._run(0, '{"name": "thing"}') == ({"name": "thing"}, GH_OK)

    def test_empty_stdout_is_not_a_failure(self):
        """gh exiting 0 with no output is an empty result, not an error."""
        from dashboard.routes.github import GH_OK
        assert self._run(0, "   ") == (None, GH_OK)

    @pytest.mark.parametrize(
        "stderr",
        [
            pytest.param(  # `gh repo view` on an absent repo: the GraphQL wording
                "GraphQL: Could not resolve to a Repository with the name 'x/y'. (repository)",
                id="graphql_missing_repo",
            ),
            pytest.param(  # `gh api` on an absent repo: the REST wording
                "gh: Not Found (HTTP 404)",
                id="rest_404",
            ),
        ],
    )
    def test_missing_target_is_not_found(self, stderr):
        from dashboard.routes.github import GH_NOT_FOUND
        data, failure = self._run(1, stderr=stderr)
        assert (data, failure) == (None, GH_NOT_FOUND)

    @pytest.mark.parametrize(
        "stderr",
        [
            # An auth lapse must never be reported as 'repo not on GitHub'.
            pytest.param("gh: Bad credentials (HTTP 401)", id="auth_failure"),
            pytest.param("API rate limit exceeded (HTTP 403)", id="rate_limit"),
            # The #6749 failure mode itself: a bad --json field is a real error.
            pytest.param('Unknown JSON field: "repository"', id="unknown_field"),
        ],
    )
    def test_real_failure_is_error_not_missing(self, stderr):
        from dashboard.routes.github import GH_ERROR
        assert self._run(1, stderr=stderr)[1] == GH_ERROR

    def test_timeout_is_error(self):
        from dashboard.routes.github import _gh_call, GH_ERROR
        with patch("dashboard.routes.github.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=10)):
            assert _gh_call(["api", "/user"]) == (None, GH_ERROR)



class TestNonPrFetchErrors:
    """Every gh loop in _fetch_github_data must report its own failures."""

    @staticmethod
    def _fetch(failing):
        """Run a fetch where `failing` names the call type that errors."""
        from dashboard.routes.github import _fetch_github_data, GH_OK, GH_ERROR

        def mock_gh_call(args, timeout=30, **_):
            if args == ["api", "/user"]:
                return ({"login": "testuser"}, GH_OK)
            if args[0] == "repo" and args[1] == "view":
                if failing == "repo":
                    return (None, GH_ERROR)
                return ({
                    "name": args[2].split("/")[1],
                    "isArchived": False,
                    # Recent enough to pull the commits branch into play.
                    "pushedAt": datetime.utcnow().isoformat() + "Z",
                    "defaultBranchRef": {"name": "main"},
                }, GH_OK)
            if args[0] == "pr":
                return ([], GH_OK)
            blob = str(args)
            if "actions/runs" in blob:
                return (None, GH_ERROR) if failing == "runs" else ({"workflow_runs": []}, GH_OK)
            if "/branches" in blob:
                return (None, GH_ERROR) if failing == "branches" else ([], GH_OK)
            if "/commits" in blob:
                return (None, GH_ERROR) if failing == "commits" else ([], GH_OK)
            return ([], GH_OK)

        with patch("dashboard.routes.github._gh_call", side_effect=mock_gh_call):
            with patch("dashboard.routes.github._gh_json",
                       side_effect=lambda a, timeout=30: mock_gh_call(a, timeout)[0]):
                with patch("dashboard.routes.github._get_tracked_repo_names", return_value=["repo-a"]):
                    return _fetch_github_data()

    @pytest.mark.parametrize(
        "failing, key, label",
        [
            # A rate-limited runs endpoint must not silently blank Failing CI.
            pytest.param("runs", "workflow_runs", "CI runs", id="ci_runs"),
            pytest.param("branches", "branches", "branches", id="branches"),
            pytest.param("commits", "recent_commits", "recent commits", id="commits"),
        ],
    )
    def test_call_failure_is_reported(self, failing, key, label):
        result = self._fetch(failing)

        assert result[key] == []
        assert any(label in e for e in result["fetch_errors"])

    def test_healthy_fetch_reports_no_errors(self):
        """The all-green path must not manufacture errors."""
        result = self._fetch(None)

        assert result["fetch_errors"] == []
        assert result["summary"]["fetch_errors"] == 0


class TestOtherGhPrListCallSites:
    """Every `gh pr list --json` call site needs a validated field constant:
    gh rejects the whole call on one bad field (#6749)."""

    def test_no_inline_gh_pr_list_json_strings_remain(self):
        """New `gh pr list --json` call sites must use a guarded constant.

        Catches a future hardcoded field string added to either module,
        which is how both existing call sites started.
        """
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        sources = [*(root / "dashboard").rglob("*.py"), *(root / "scripts").rglob("*.py")]
        # The code these call sites live in has moved before (#8771); a scan of a stale path passes vacuously.
        assert root / "dashboard" / "routes" / "github.py" in sources
        offenders = []
        for path in sources:
            # A --json argument given as a comma-joined literal, e.g.
            # "--json", "number,title,author"  — as opposed to ",".join(CONST).
            for match in re.finditer(r'"--json",\s*\n?\s*"([a-zA-Z]+,[a-zA-Z,]+)"', path.read_text()):
                offenders.append(f"{path.relative_to(root)}: {match.group(1)[:60]}")

        assert not offenders, (
            "Inline --json field strings found; use a validated constant instead:\n  "
            + "\n  ".join(offenders)
        )


class TestRepoViewFields:
    """`gh repo view` has its own field set — same all-or-nothing contract."""

    def test_requested_fields_are_all_valid(self):
        from dashboard.routes.github import REPO_VIEW_FIELDS, GH_REPO_VIEW_VALID_FIELDS

        invalid = set(REPO_VIEW_FIELDS) - GH_REPO_VIEW_VALID_FIELDS
        assert not invalid, (
            f"Invalid `gh repo view --json` field(s): {sorted(invalid)}. "
            "gh rejects the whole call, so every repo would look absent."
        )

    def test_field_sets_are_not_interchangeable(self):
        """Guards the assumption that broke #6749: sets differ per subcommand."""
        from dashboard.routes.github import GH_PR_LIST_VALID_FIELDS, GH_REPO_VIEW_VALID_FIELDS

        assert GH_PR_LIST_VALID_FIELDS != GH_REPO_VIEW_VALID_FIELDS
        # `repository` is in neither — it belongs to `gh search prs`.
        assert "repository" not in GH_PR_LIST_VALID_FIELDS
        assert "repository" not in GH_REPO_VIEW_VALID_FIELDS

    def test_fields_cover_what_fetch_uses(self):
        """Keys _fetch_github_data and the frontend read must be requested."""
        from dashboard.routes.github import REPO_VIEW_FIELDS

        for field in ("name", "isArchived", "pushedAt", "defaultBranchRef",
                      "url", "isPrivate", "description", "stargazerCount"):
            assert field in REPO_VIEW_FIELDS

    def test_requested_fields_still_accepted_by_live_gh(self):
        """Removal direction only — additions are gh's business, not a failure."""
        import pytest
        import shutil
        from dashboard.routes.github import REPO_VIEW_FIELDS

        gh = shutil.which("gh")
        if not gh:
            pytest.skip("gh CLI unavailable")
        proc = subprocess.run(
            [gh, "repo", "view", "cli/cli", "--json", "__bogus__"],
            capture_output=True, text=True, timeout=30,
        )
        text = proc.stdout + proc.stderr
        if "Available fields:" not in text:
            pytest.skip("gh output format changed")
        live = {l.strip() for l in text.split("Available fields:", 1)[1].splitlines() if l.strip()}

        missing = set(REPO_VIEW_FIELDS) - live
        assert not missing, (
            f"The installed gh no longer accepts: {sorted(missing)}. "
            "Every repo fetch would fail. Update REPO_VIEW_FIELDS in dashboard/app.py."
        )


class TestParallelFetch:
    """#8093: every stage runs its gh calls in parallel, and the output keeps the
    order a one-at-a-time fetch produces."""

    STAGES = ("repo view", "pr list", "/commits", "actions/runs", "/branches")

    @classmethod
    def _fake(cls, missing=(), errors=(), meet=False, seen=None):
        """A gh stand-in. With meet=True the first two calls of each stage wait
        for each other at a barrier, which only succeeds if they run at once;
        a one-at-a-time fetch times out there instead of hanging."""
        import threading
        from dashboard.routes.github import GH_NOT_FOUND, GH_OK, GH_ERROR

        lock = threading.Lock()
        barriers = {kind: threading.Barrier(2, timeout=2) for kind in cls.STAGES}
        arrivals = {kind: 0 for kind in cls.STAGES}
        state = {"met": set(), "alone": set()}

        def call(args, timeout=30, **_):
            text = " ".join(args)
            kind = next((k for k in cls.STAGES if k in text), None)
            with lock:
                if seen is not None:
                    seen.append(args)
                first_two = kind is not None and arrivals[kind] < 2
                if kind is not None:
                    arrivals[kind] += 1
            if meet and first_two:
                try:
                    barriers[kind].wait()
                    with lock:
                        state["met"].add(kind)
                except threading.BrokenBarrierError:
                    with lock:
                        state["alone"].add(kind)
            if args == ["api", "/user"]:
                return ({"login": "me"}, GH_OK)
            for name in errors:
                if f"me/{name}" in text and "branches" in text:
                    return (None, GH_ERROR)
            if args[:2] == ["repo", "view"]:
                name = args[2].split("/", 1)[1]
                if name in missing:
                    return (None, GH_NOT_FOUND)
                return ({"name": name, "isArchived": False,
                         "pushedAt": "2999-01-01T00:00:00Z",
                         "defaultBranchRef": {"name": "main"}}, GH_OK)
            if args[0] == "pr":
                repo = args[args.index("--repo") + 1].split("/", 1)[1]
                return ([{"title": f"pr-{repo}", "headRefName": "x"}], GH_OK)
            if "actions/runs" in text:
                return ({"workflow_runs": []}, GH_OK)
            return ([], GH_OK)

        return call, state

    def test_output_order_matches_the_tracked_order(self):
        from dashboard.routes.github import _fetch_github_data

        names = [f"r{i:02d}" for i in range(12)]
        call, state = self._fake(errors=("r03", "r07"), meet=True)
        with patch("dashboard.routes.github._gh_call", side_effect=call), \
                patch("dashboard.routes.github._get_tracked_repo_names", return_value=names):
            result = _fetch_github_data()

        assert [r["name"] for r in result["repos"]] == names
        assert [p["repository"]["name"] for p in result["open_pull_requests"]] == names
        assert result["fetch_errors"] == ["branches for me/r03", "branches for me/r07"]
        assert state["alone"] == set(), f"these stages ran one call at a time: {state['alone']}"
        assert state["met"] == set(self.STAGES)

    def test_an_empty_pr_list_response_is_no_prs(self):
        from dashboard.routes.github import GH_OK, _fetch_github_data

        def call(args, timeout=30, **_):
            if args == ["api", "/user"]:
                return ({"login": "me"}, GH_OK)
            if args[:2] == ["repo", "view"]:
                return ({"name": "r", "isArchived": False, "pushedAt": ""}, GH_OK)
            if args[0] == "pr":
                return (None, GH_OK)  # gh printed nothing
            return ([], GH_OK)

        with patch("dashboard.routes.github._gh_call", side_effect=call), \
                patch("dashboard.routes.github._get_tracked_repo_names", return_value=["r"]):
            result = _fetch_github_data()

        assert result["open_pull_requests"] == []
        assert result["fetch_errors"] == []



class TestFetchStages:
    """Stage filters and content, pinned so the parallel rewrite can't drift."""

    @staticmethod
    def _call(repos, seen, view_error=(), view_missing=(), boom=None):
        from dashboard.routes.github import GH_ERROR, GH_NOT_FOUND, GH_OK

        def call(args, timeout=30, **_):
            seen.append(args)
            text = " ".join(args)
            if boom and boom in text:
                raise RuntimeError("worker blew up")
            if args == ["api", "/user"]:
                return ({"login": "me"}, GH_OK)
            if args[:2] == ["repo", "view"]:
                name = args[2].split("/", 1)[1]
                if name in view_error:
                    return (None, GH_ERROR)
                if name in view_missing:
                    return (None, GH_NOT_FOUND)
                return (dict(repos[name], name=name), GH_OK)
            if args[0] == "pr":
                return ([], GH_OK)
            if "/commits" in text:
                return ([{"sha": f"{i:040d}", "html_url": "u",
                          "commit": {"message": f"m{i}\nbody", "author": {"name": "a", "date": "d"}}}
                         for i in range(25)], GH_OK)
            if "actions/runs" in text:
                return ({"workflow_runs": [{"name": "ci", "status": "completed", "conclusion": "failure",
                                            "head_branch": "main", "created_at": "c", "html_url": "h"}]}, GH_OK)
            if "branches" in text:
                return ([{"name": "main", "protected": True}], GH_OK)
            return (None, GH_ERROR)

        return call

    def _fetch(self, names, call):
        from dashboard.routes.github import _fetch_github_data

        with patch("dashboard.routes.github._gh_call", side_effect=call), \
                patch("dashboard.routes.github._get_tracked_repo_names", return_value=names):
            return _fetch_github_data()

    def test_filters_and_content_per_stage(self):
        repos = {
            "live": {"isArchived": False, "pushedAt": "2999-01-01T00:00:00Z", "defaultBranchRef": None},
            "stale": {"isArchived": False, "pushedAt": "2000-01-01T00:00:00Z"},
            "never": {"isArchived": False, "pushedAt": None},
            "old": {"isArchived": True, "pushedAt": "2999-01-01T00:00:00Z"},
        }
        seen = []
        result = self._fetch(list(repos), self._call(repos, seen))
        text = [" ".join(a) for a in seen]

        # Archived repos get no PR, commit, CI or branch calls.
        assert not any("me/old" in t and not t.startswith("repo view") for t in text)
        # Only a repo pushed in the last week has its commits fetched, on the
        # default branch, falling back to main.
        commit_calls = [t for t in text if "/commits" in t]
        assert commit_calls == [t for t in commit_calls if "/repos/me/live/commits" in t]
        assert len(commit_calls) == 1 and "sha=main" in commit_calls[0]
        assert len(result["recent_commits"]) == 20
        assert result["recent_commits"][0]["message"] == "m0"
        assert result["recent_commits"][0]["sha"] == "0000000"
        assert [r["repo"] for r in result["workflow_runs"]] == ["live", "stale", "never"]
        assert result["workflow_runs"][0]["conclusion"] == "failure"
        assert result["branches"][0] == {"repo": "live", "name": "main", "protected": True}
        assert result["summary"]["archived_repos"] == 1
        assert result["summary"]["failing_ci"] == 3

    def test_a_failed_repo_view_is_a_fetch_error_and_asked_again(self):
        repos = {"flaky": {"isArchived": False, "pushedAt": ""}}
        seen = []
        call = self._call(repos, seen, view_error=("flaky",))
        first = self._fetch(["flaky"], call)
        second = self._fetch(["flaky"], call)
        views = [a for a in seen if a[:2] == ["repo", "view"]]
        assert len(views) == 2
        assert first["fetch_errors"] == second["fetch_errors"] == ["repo metadata for me/flaky"]
        assert first["summary"]["repos_not_on_github"] == 0

    def test_a_repo_that_turns_missing_recovers_on_the_next_refresh(self):
        """Token scope loss: private repos turn 'not found'; nothing about it is remembered."""
        repos = {"private": {"isArchived": False, "pushedAt": ""}}
        seen = []
        self._fetch(["private"], self._call(repos, seen))
        lost = self._fetch(["private"], self._call(repos, seen, view_missing=("private",)))
        back = self._fetch(["private"], self._call(repos, seen))
        assert lost["summary"]["repos_not_on_github"] == 1
        assert [r["name"] for r in back["repos"]] == ["private"]
        assert len([a for a in seen if a[:2] == ["repo", "view"]]) == 3

    def test_an_exception_in_a_worker_escapes_the_fetch(self):
        repos = {"a": {"isArchived": False, "pushedAt": ""}, "b": {"isArchived": False, "pushedAt": ""}}
        seen = []
        with pytest.raises(RuntimeError, match="worker blew up"):
            self._fetch(["a", "b"], self._call(repos, seen, boom="/repos/me/b/branches"))


def test_only_an_expected_not_found_skips_the_warning(caplog):
    """repo view passes missing_ok (half the tracked projects aren't on GitHub);
    a 404 anywhere else is a real failure and still warns."""
    import logging
    from dashboard.routes.github import GH_NOT_FOUND, _gh_call

    missing = MagicMock(returncode=1, stdout="", stderr="GraphQL: Could not resolve to a Repository")
    with caplog.at_level(logging.DEBUG):
        with patch("dashboard.routes.github.subprocess.run", return_value=missing):
            assert _gh_call(["repo", "view", "me/local-only"], missing_ok=True)[1] == GH_NOT_FOUND
            assert _gh_call(["api", "/repos/me/x/actions/runs"])[1] == GH_NOT_FOUND
    warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("me/local-only" in m for m in warned)
    assert any("/repos/me/x/actions/runs" in m for m in warned)
    assert any("me/local-only" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


def test_the_repo_view_stage_marks_not_found_as_expected():
    from dashboard.routes.github import GH_OK, _fetch_github_data

    kwargs_by_kind = {}

    def call(args, timeout=30, **kw):
        kwargs_by_kind.setdefault(" ".join(args[:2]), kw)
        if args == ["api", "/user"]:
            return ({"login": "me"}, GH_OK)
        if args[:2] == ["repo", "view"]:
            return ({"name": "r", "isArchived": False, "pushedAt": ""}, GH_OK)
        return ([], GH_OK)

    with patch("dashboard.routes.github._gh_call", side_effect=call), \
            patch("dashboard.routes.github._get_tracked_repo_names", return_value=["r"]):
        _fetch_github_data()
    assert kwargs_by_kind["repo view"] == {"missing_ok": True}
    assert all(kw == {} for kind, kw in kwargs_by_kind.items() if kind != "repo view")


def test_missing_ok_still_warns_on_a_real_failure(caplog):
    import logging
    from dashboard.routes.github import GH_ERROR, _gh_call

    broken = MagicMock(returncode=1, stdout="", stderr="HTTP 502: Bad Gateway")
    with caplog.at_level(logging.DEBUG):
        with patch("dashboard.routes.github.subprocess.run", return_value=broken):
            assert _gh_call(["repo", "view", "me/flaky"], missing_ok=True)[1] == GH_ERROR
    assert any("me/flaky" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_each_refresh_logs_one_missing_count_line(caplog):
    import logging
    from dashboard.routes.github import GH_NOT_FOUND, GH_OK, _fetch_github_data

    def call(args, timeout=30, **_):
        if args == ["api", "/user"]:
            return ({"login": "me"}, GH_OK)
        if args[:2] == ["repo", "view"]:
            if args[2] in ("me/local-a", "me/local-b"):
                return (None, GH_NOT_FOUND)
            return ({"name": "here", "isArchived": False, "pushedAt": ""}, GH_OK)
        return ([], GH_OK)

    with caplog.at_level(logging.DEBUG):
        with patch("dashboard.routes.github._gh_call", side_effect=call), \
                patch("dashboard.routes.github._get_tracked_repo_names", return_value=["local-a", "here", "local-b"]):
            _fetch_github_data()
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert info.count("GitHub: 2 of 3 tracked projects not on GitHub") == 1
    per_repo = [r for r in caplog.records if "is not on GitHub" in r.getMessage()]
    assert per_repo and all(r.levelno == logging.DEBUG for r in per_repo)
