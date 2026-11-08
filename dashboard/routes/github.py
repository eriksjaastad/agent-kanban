"""GET /api/github: open PRs and repo activity for the tracked projects, through the gh CLI (#5373)."""

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from db.manager import DatabaseManager
from fastapi import APIRouter

from dashboard.common import logger
from dashboard.github_cache import GitHubCache
from scripts.config import projects_root as config_projects_root

router = APIRouter()


_github_cache = GitHubCache()


def _find_gh() -> str:
    """Find gh binary, checking common install locations if not on PATH.

    launchd services have a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin),
    so shutil.which() alone won't find Homebrew-installed gh.
    """
    found = shutil.which("gh") or os.environ.get("GH_PATH")
    if found:
        return found
    # Check standard install locations (launchd PATH doesn't include these)
    homebrew_paths = [
        os.path.expanduser("~/.local/bin/gh"),
        os.path.join(os.environ.get("HOMEBREW_PREFIX", "/opt/homebrew"), "bin", "gh"),
    ]
    for candidate in homebrew_paths:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "gh"


_GH_BIN = _find_gh()


# Fields requested from `gh pr list --json`. Every name here must appear in
# GH_PR_LIST_VALID_FIELDS — `gh` rejects the whole call on one bad field, which
# silently emptied the dashboard's PR panel until #6749.
PR_LIST_FIELDS = (
    "title",
    "number",
    "url",
    "author",
    "createdAt",
    "statusCheckRollup",
    "reviewDecision",
    "headRefName",
    "baseRefName",
    "isDraft",
)


# Valid `gh pr list --json` field names, as reported by gh 2.96.0. Note this is
# NOT the `gh search prs` field set — that one has `repository`, this one does
# not, because `pr list` is already scoped to a single repo.
GH_PR_LIST_VALID_FIELDS = frozenset({
    "additions", "assignees", "author", "autoMergeRequest", "baseRefName",
    "baseRefOid", "body", "changedFiles", "closed", "closedAt",
    "closingIssuesReferences", "comments", "commits", "createdAt", "deletions",
    "files", "fullDatabaseId", "headRefName", "headRefOid", "headRepository",
    "headRepositoryOwner", "id", "isCrossRepository", "isDraft", "labels",
    "latestReviews", "maintainerCanModify", "mergeCommit", "mergeStateStatus",
    "mergeable", "mergedAt", "mergedBy", "milestone", "number",
    "potentialMergeCommit", "projectCards", "projectItems", "reactionGroups",
    "reviewDecision", "reviewRequests", "reviews", "state", "statusCheckRollup",
    "title", "updatedAt", "url",
})


# gh signals "this repo does not exist" differently per transport: the GraphQL
# path (`gh repo view`) and the REST path (`gh api`). Anything else is a real
# failure — auth lapse, rate limit, network — and must not be reported as
# "not on GitHub".
_GH_NOT_FOUND_MARKERS = (
    "could not resolve to a repository",
    "http 404",
)


# _gh_call failure kinds.
GH_OK = None


GH_NOT_FOUND = "not_found"


GH_ERROR = "error"


# Fields requested from `gh repo view --json`. Same all-or-nothing contract as
# PR_LIST_FIELDS — and a different valid set, since field sets are per-subcommand.
REPO_VIEW_FIELDS = (
    "name",
    "url",
    "pushedAt",
    "defaultBranchRef",
    "isPrivate",
    "description",
    "stargazerCount",
    "forkCount",
    "isArchived",
)


# Valid `gh repo view --json` field names, as reported by gh 2.96.0.
GH_REPO_VIEW_VALID_FIELDS = frozenset({
    "archivedAt", "assignableUsers", "codeOfConduct", "contactLinks", "createdAt",
    "defaultBranchRef", "deleteBranchOnMerge", "description", "diskUsage",
    "forkCount", "fundingLinks", "hasDiscussionsEnabled", "hasIssuesEnabled",
    "hasProjectsEnabled", "hasWikiEnabled", "homepageUrl", "id", "isArchived",
    "isBlankIssuesEnabled", "isEmpty", "isFork", "isInOrganization", "isMirror",
    "isPrivate", "isSecurityPolicyEnabled", "isTemplate",
    "isUserConfigurationRepository", "issueTemplates", "issues", "labels",
    "languages", "latestRelease", "licenseInfo", "mentionableUsers",
    "mergeCommitAllowed", "milestones", "mirrorUrl", "name", "nameWithOwner",
    "openGraphImageUrl", "owner", "parent", "primaryLanguage", "projects",
    "projectsV2", "pullRequestTemplates", "pullRequests", "pushedAt",
    "rebaseMergeAllowed", "repositoryTopics", "securityPolicyUrl",
    "squashMergeAllowed", "sshUrl", "stargazerCount", "templateRepository",
    "updatedAt", "url", "usesCustomOpenGraphImage", "viewerCanAdminister",
    "viewerDefaultCommitEmail", "viewerDefaultMergeMethod", "viewerHasStarred",
    "viewerPermission", "viewerPossibleCommitEmails", "viewerSubscription",
    "visibility", "watchers",
})


def _gh_call(args: List[str], timeout: int = 30, missing_ok: bool = False) -> tuple:
    """Run a gh CLI command, returning (parsed_json, failure_kind).

    failure_kind is GH_OK on success, GH_NOT_FOUND when the target genuinely
    does not exist, or GH_ERROR for any other failure. Callers need the
    distinction: a missing repo is a fact about the world, while an auth or
    rate-limit failure means our data is incomplete and we must say so.

    missing_ok is for a caller that expects some targets not to exist and
    counts them itself (the repo view of every tracked project): its
    not-found is logged at debug, not as a warning. Every other failure warns.

    stderr is logged but never returned — it can contain tokens and auth URLs,
    and callers surface these values to the browser.
    """
    try:
        result = subprocess.run(
            [_GH_BIN] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            blob = (stderr + " " + result.stdout).lower()
            not_found = any(marker in blob for marker in _GH_NOT_FOUND_MARKERS)
            log = logger.debug if (not_found and missing_ok) else logger.warning
            log(f"gh command failed: gh {' '.join(args)} — {stderr}")
            return None, (GH_NOT_FOUND if not_found else GH_ERROR)
        if not result.stdout.strip():
            return None, GH_OK
        return json.loads(result.stdout), GH_OK
    except FileNotFoundError:
        logger.error("gh CLI not found on PATH")
        return None, GH_ERROR
    except subprocess.TimeoutExpired:
        logger.warning(f"gh command timed out: gh {' '.join(args)}")
        return None, GH_ERROR
    except json.JSONDecodeError as e:
        logger.warning(f"gh returned non-JSON: {e}")
        return None, GH_ERROR


def _gh_json(args: List[str], timeout: int = 30) -> any:
    """Run a gh CLI command and return parsed JSON, or None on failure.

    Thin wrapper over _gh_call for callers that do not need to distinguish a
    missing target from a failed fetch.
    """
    return _gh_call(args, timeout)[0]


def _get_tracked_repo_names() -> List[str]:
    """Get repo names from the kanban board's project list.

    Includes top-level projects and subdirectories of github-repos/.
    Excludes internal names: anything starting with an underscore.

    A database failure raises: an empty list would read as "no tracked
    projects", and the GitHub cache already turns a raised refresh into
    refresh_error while keeping the previous snapshot.
    """
    db = DatabaseManager()
    projects = db.get_all_projects()
    names: List[str] = []
    for p in projects:
        pid = p.get("id", "")
        # Skip internal/infrastructure projects
        if pid.startswith("_") or pid.startswith("__"):
            continue
        # github-repos is a container — its children are the actual repos
        if pid == "github-repos":
            github_repos_dir = config_projects_root() / "github-repos"
            if github_repos_dir.is_dir():
                for child in github_repos_dir.iterdir():
                    if child.is_dir() and not child.name.startswith(".") and not child.name.startswith("00_"):
                        names.append(child.name)
            continue
        names.append(pid)
    return sorted(set(names))


# gh calls run a few at a time; GitHub's secondary rate limit allows far more.
_GH_WORKERS = 6


def _gh_map(fn, items: List) -> List:
    """Run fn over items on a few threads; results come back in input order."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=_GH_WORKERS, thread_name_prefix="gh") as pool:
        return list(pool.map(fn, items))


def _fetch_github_data() -> Dict:
    """Fetch GitHub data for tracked projects only (from kanban board).

    Each stage's calls run in parallel, and the snapshot is assembled in the
    same order a one-at-a-time fetch would produce.
    """

    # 1. Account info
    user = _gh_json(["api", "/user"])
    if user is None:
        return {"error": "gh CLI not available or not authenticated", "cached": False}

    owner = user.get("login", "")

    # 2. Get repo list from kanban board, then fetch details from GitHub
    tracked_names = _get_tracked_repo_names()
    if not tracked_names:
        return {"error": "No tracked projects found in kanban board", "cached": False}

    fetch_errors: List[str] = []

    repos: List[Dict] = []
    repos_missing = 0
    # About half the tracked projects aren't on GitHub (third-party checkouts in
    # github-repos/, local-only projects), so their not-found is expected and
    # counted below rather than logged as a warning every refresh.
    views = _gh_map(lambda name: _gh_call([
        "repo", "view", f"{owner}/{name}",
        "--json", ",".join(REPO_VIEW_FIELDS),
    ], timeout=10, missing_ok=True), tracked_names)
    for name, (repo_info, failure) in zip(tracked_names, views):
        if repo_info:
            repos.append(repo_info)
        elif failure == GH_NOT_FOUND:
            # CAVEAT: GitHub answers "no such repo" and "you cannot see this
            # repo" identically, by design, so it never leaks a private repo's
            # existence. A token that loses `repo` scope therefore makes every
            # private repo look absent rather than unreachable, and gh gives us
            # nothing to tell the two apart.
            #
            # We cannot resolve that here — but we refuse to let it be silent:
            # this count is surfaced in the dashboard summary, so losing access
            # shows up as a jump in "Not on GitHub" rather than as nothing.
            repos_missing += 1
            logger.debug(f"Tracked project '{name}' is not on GitHub (or not visible to this token)")
        else:
            # A failed fetch drops this repo from `repos` entirely, taking its
            # PRs, CI and branches with it. Counting that as "not on GitHub"
            # would assert a healthy fetch that never happened.
            fetch_errors.append(f"repo metadata for {owner}/{name}")

    # One line per refresh keeps a jump visible in the log (a token losing
    # `repo` scope makes private repos look absent; see the CAVEAT above).
    logger.info(f"GitHub: {repos_missing} of {len(tracked_names)} tracked projects not on GitHub")

    # 3. Open PRs across tracked repos
    all_prs: List[Dict] = []
    active = [r for r in repos if not r.get("isArchived")]
    pr_lists = _gh_map(lambda repo: _gh_call([
        "pr", "list",
        "--repo", f"{owner}/{repo.get('name', '')}",
        "--json", ",".join(PR_LIST_FIELDS),
        "--state", "open",
        "--limit", "50",
    ], timeout=15), active)
    for repo, (prs, failure) in zip(active, pr_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            # Distinguish a failed fetch from a repo with no open PRs. Without
            # this the UI renders an empty PR panel either way, which is how a
            # bad --json field went unnoticed (see #6749).
            fetch_errors.append(f"open PRs for {owner}/{repo_name}")
            continue
        # `gh pr list` is already scoped to one repo, so it has no `repository`
        # field to return. Attach it here — consumers key PRs by repo name.
        for pr in prs or []:
            pr["repository"] = {"name": repo_name}
        all_prs.extend(prs or [])

    # 4. Recent commits (last 7 days) — only from tracked repos
    # Use timezone-aware UTC and a Zulu timestamp GitHub accepts. Avoid
    # datetime.utcnow().isoformat()+"Z" (naive + micros) which made string
    # comparisons against GitHub's pushedAt brittle.
    seven_days_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_commits: List[Dict] = []
    recent = [r for r in active if r.get("pushedAt") and r["pushedAt"] >= seven_days_ago]
    commit_lists = _gh_map(lambda repo: _gh_call([
        "api", f"/repos/{owner}/{repo.get('name', '')}/commits",
        "-q", ".",
        "--method", "GET",
        "-f", f"since={seven_days_ago}",
        "-f", f"sha={(repo.get('defaultBranchRef') or {}).get('name', 'main')}",
        "-f", "per_page=20",
    ], timeout=15), recent)
    for repo, (commits, failure) in zip(recent, commit_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            fetch_errors.append(f"recent commits for {owner}/{repo_name}")
        if commits and isinstance(commits, list):
            for c in commits[:20]:
                recent_commits.append({
                    "repo": repo_name,
                    "sha": c.get("sha", "")[:7],
                    "message": (c.get("commit", {}).get("message") or "").split("\n")[0],
                    "author": (c.get("commit", {}).get("author") or {}).get("name", ""),
                    "date": (c.get("commit", {}).get("author") or {}).get("date", ""),
                    "url": c.get("html_url", ""),
                })

    # 5. Workflow runs (CI status) — recent runs across tracked repos
    workflow_runs: List[Dict] = []
    run_lists = _gh_map(lambda repo: _gh_call([
        "api", f"/repos/{owner}/{repo.get('name', '')}/actions/runs",
        "-q", ".",
        "--method", "GET",
        "-f", "per_page=5",
    ], timeout=10), active)
    for repo, (runs, failure) in zip(active, run_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            # Without this a rate-limited runs endpoint empties workflow_runs,
            # silently blanking the Failing CI panel and undercounting failing_ci.
            fetch_errors.append(f"CI runs for {owner}/{repo_name}")
        if runs and isinstance(runs, dict):
            for run in (runs.get("workflow_runs") or [])[:5]:
                workflow_runs.append({
                    "repo": repo_name,
                    "name": run.get("name", ""),
                    "status": run.get("status", ""),
                    "conclusion": run.get("conclusion"),
                    "branch": run.get("head_branch", ""),
                    "created_at": run.get("created_at", ""),
                    "url": run.get("html_url", ""),
                })

    # 6. Branch info — only tracked repos
    branch_info: List[Dict] = []
    branch_lists = _gh_map(lambda repo: _gh_call([
        "api", f"/repos/{owner}/{repo.get('name', '')}/branches",
        "-q", ".",
        "--method", "GET",
        "-f", "per_page=100",
    ], timeout=10), active)
    for repo, (branches, failure) in zip(active, branch_lists):
        repo_name = repo.get("name", "")
        if failure is not GH_OK:
            fetch_errors.append(f"branches for {owner}/{repo_name}")
        if branches and isinstance(branches, list):
            for b in branches:
                branch_info.append({
                    "repo": repo_name,
                    "name": b.get("name", ""),
                    "protected": b.get("protected", False),
                })

    return {
        "user": {
            "login": user.get("login"),
            "name": user.get("name"),
            "avatar_url": user.get("avatar_url"),
            "public_repos": user.get("public_repos"),
            "private_repos": user.get("total_private_repos"),
            "followers": user.get("followers"),
            "following": user.get("following"),
        },
        "repos": repos,
        "tracked_projects": tracked_names,
        "open_pull_requests": all_prs,
        "recent_commits": recent_commits,
        "workflow_runs": workflow_runs,
        "branches": branch_info,
        "fetch_errors": fetch_errors,
        "summary": {
            "total_repos": len(repos),
            "tracked_projects": len(tracked_names),
            "repos_found_on_github": len(repos),
            "repos_not_on_github": repos_missing,
            "archived_repos": sum(1 for r in repos if r.get("isArchived")),
            "open_prs": len(all_prs),
            "draft_prs": sum(1 for p in all_prs if p.get("isDraft")),
            "recent_commit_count": len(recent_commits),
            "repos_with_ci": len(set(r["repo"] for r in workflow_runs)),
            "fetch_errors": len(fetch_errors),
            "failing_ci": sum(
                1 for r in workflow_runs
                if r.get("conclusion") == "failure"
                and (r.get("branch") in ("main", "master")
                     or any(pr.get("headRefName") == r.get("branch")
                            and pr.get("repository", {}).get("name") == r.get("repo")
                            for pr in all_prs))
            ),
        },
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "cached": False,
    }


# Past weeks never change, so merged-PR counts refresh slowly. A new list or a
# new week replaces the cache, so the split recalculates at once.
_merged = {"key": None, "cache": None}


def merged_prs_weekly(user_facing: frozenset, starts: List) -> Dict:
    """Merged PRs per week (local Monday starts), user-facing repos vs the rest.

    Returns at once, like /api/github: ``weeks`` arrives once the background
    count lands, alongside the cache's refresh state.
    """
    key = (user_facing, tuple(starts))
    if _merged["key"] != key:
        _merged["key"], _merged["cache"] = key, GitHubCache(ttl=900, label="Merged PRs")
    return _merged["cache"].read(lambda: _fetch_merged_weekly(user_facing, starts))


def _fetch_merged_weekly(user_facing: frozenset, starts: List) -> Dict:
    """All merged PRs per week come from search counts (one call a week, under
    the search API's 1000-result cap); user-facing repos list their own merges.
    """
    user = _gh_json(["api", "/user"])
    if user is None:
        return {"error": "gh CLI not available or not authenticated"}
    owner = user["login"]
    # Each boundary gets its own UTC offset, so a DST change lands on the right day.
    edges = [datetime.combine(s, datetime.min.time()).astimezone()
             for s in [*starts, starts[-1] + timedelta(weeks=1)]]
    totals = _gh_map(lambda i: _gh_call([
        "api", "-X", "GET", "search/issues", "--jq", ".total_count", "-f", "per_page=1",
        "-f", f"q=user:{owner} is:pr is:merged merged:{edges[i].isoformat()}"
              f"..{(edges[i + 1] - timedelta(seconds=1)).isoformat()}",
    ], timeout=15), range(len(starts)))
    names = sorted(user_facing)
    lists = _gh_map(lambda name: _gh_call([
        # No --search: through search a missing repo comes back as an empty list.
        "pr", "list", "--repo", f"{owner}/{name}", "--state", "merged", "--json", "mergedAt",
        "--limit", "1000",
    ], timeout=60, missing_ok=True), names)
    if any(failure is not GH_OK for _, failure in totals):
        raise ValueError("merged PR search failed")
    user_facing_counts = {s: 0 for s in starts}
    not_on_github = []
    for name, (prs, failure) in zip(names, lists):
        if failure == GH_NOT_FOUND:
            not_on_github.append(name)
            continue
        merged_at = [datetime.fromisoformat(pr["mergedAt"]).astimezone() for pr in prs or []]
        # A full page that still reaches into the window may have cut some off.
        if failure is not GH_OK or (len(merged_at) >= 1000 and min(merged_at) >= edges[0]):
            raise ValueError(f"merged PRs for {name} failed or hit the 1000 limit")
        for when in merged_at:
            merged = when.date()
            week = merged - timedelta(days=merged.weekday())
            if week in user_facing_counts:
                user_facing_counts[week] += 1
    return {
        "weeks": [{"week_start": s.isoformat(), "user_facing": user_facing_counts[s],
                   "internal": total - user_facing_counts[s]}
                  for s, (total, _) in zip(starts, totals)],
        "not_on_github": not_on_github,
    }


@router.get("/api/github")
async def api_github():
    """Return the last snapshot immediately while one worker refreshes GitHub.

    A cold response has no repository data and ``refreshing: true``. Clients
    poll using retry_after_seconds until the snapshot arrives. Failed refreshes
    preserve old data and expose refresh_error rather than emptying the page.
    """
    return _github_cache.read(_fetch_github_data)
