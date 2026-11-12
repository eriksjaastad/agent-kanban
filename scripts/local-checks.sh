#!/usr/bin/env bash
# Local checks for project-tracker (#7828): the pytest and frontend jobs of
# .github/workflows/tests.yml.
#
# Only the shared runner (~/.claude/hooks/git-local-checks.py) runs
# this: on every push, and by hand with `~/.claude/hooks/git-local-checks.py
# --head`. It checks the commit out in a fresh worktree, sets the
# LOCAL_CHECKS_* variables and bounds the run. Every check runs on every push.
#
# pytest syncs its own environment inside this throwaway worktree. The frontend
# job runs in a throwaway export of dashboard/, with its packages installed by
# npm ci from the local npm cache only (see npm_ci_offline): exactly the
# locked packages, no network, no install scripts.
set -euo pipefail

if [ "${LOCAL_CHECKS_CLEAN:-}" != 1 ]; then
    echo "local checks: run them with the shared runner, which checks HEAD in a fresh worktree:" >&2
    echo "  ~/.claude/hooks/git-local-checks.py --head" >&2
    exit 2
fi
cd "$(git rev-parse --show-toplevel)"
sha="$(git rev-parse HEAD)"
if [ "$sha" != "$(git rev-parse "${LOCAL_CHECKS_SHA}^{commit}")" ]; then
    echo "local checks: this checkout is at $sha, not $LOCAL_CHECKS_SHA" >&2
    exit 2
fi
echo "local checks: $sha in $(pwd -P)"

UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

# Run one named check, record a failure, and return its status.
failed=()
check() {
    local name="$1" status=0
    shift
    echo "== $name"
    "$@" || status=$?
    if [ "$status" -ne 0 ]; then
        failed+=("$name")
    fi
    return "$status"
}
# A check nothing else depends on: its failure is recorded, and the run goes on.
step() { check "$@" || true; }

# npm_ci_offline DIR: install exactly what DIR's package-lock.json pins, as
# CI's npm ci did, but only from the local npm cache. --offline never touches
# the registry over the network: every tarball comes from the cache, checked
# against the lockfile's integrity hash. --ignore-scripts runs no package code
# at install. That keeps the intent of the owner's npm guard (no fresh downloads,
# no install-time package code) while testing exactly the locked packages, not
# a developer's node_modules. Both flags hold only for registry tarballs: npm
# clones a git dependency from the network and runs its prepare scripts
# regardless. So npm's git is set to false (every git dependency, from
# package.json, a lockfile or a shrinkwrap, fails without running git), and
# the lockfile is refused up front if any entry is not served by the registry
# (git, file, link, another host), or if an npm-shrinkwrap.json would
# override it. A tarball the cache cannot supply (missing, or failing its
# integrity hash, which npm treats as missing) fails with its name@version;
# there is no network fallback. Decided for the owner, 2026-10-02
# (#7828).
npm_ci_offline() {
    local out status=0
    if [ -e "$1/npm-shrinkwrap.json" ]; then
        echo "$1/npm-shrinkwrap.json would override package-lock.json; this check installs from package-lock.json only"
        return 1
    fi
    if ! node -e '
        const lock = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
        if (typeof lock.packages !== "object" || lock.packages === null) {
            console.log("package-lock.json has no packages map (lockfileVersion " + lock.lockfileVersion
                + "); regenerate it with npm 7 or later");
            process.exit(1);
        }
        const bad = Object.entries(lock.packages)
            .filter(([key, e]) => key && !e.inBundle
                && !(typeof e.resolved === "string" && e.resolved.startsWith("https://registry.npmjs.org/")))
            .map(([key, e]) => `${key}: ${e.link ? "link to " : ""}${e.resolved || "(no registry tarball)"}`);
        if (bad.length) {
            console.log("package-lock.json has entries not served by the npm registry; offline npm ci");
            console.log("cannot keep them off the network or stop their scripts, so they are refused:");
            for (const line of bad.slice(0, 20)) console.log("  " + line);
            process.exit(1);
        }
    ' "$1/package-lock.json"; then
        return 1
    fi
    out="$(cd "$1" && npm ci --offline --ignore-scripts --git=false --no-audit --no-fund 2>&1)" || status=$?
    if [ "$status" -eq 0 ]; then
        return 0
    fi
    printf '%s\n' "$out" | tail -n 25
    # Name each tarball the cache could not supply, as the lockfile names it.
    printf '%s\n' "$out" \
        | sed -n 's|.*request to \(https://[^ ]*\) failed.*|\1|p' \
        | node -e '
            const lock = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
            const missing = new Set(require("fs").readFileSync(0, "utf8").split("\n").filter(Boolean));
            const names = Object.entries(lock.packages)
                .filter(([key, e]) => key && missing.has(e.resolved))
                .map(([key, e]) => `${key.slice(key.lastIndexOf("node_modules/") + 13)}@${e.version}`);
            if (names.length) {
                console.log("not usable from the local npm cache: missing, or failing its integrity");
                console.log("check (no network fallback):");
                for (const n of [...new Set(names)].sort()) console.log("  " + n);
                console.log("  check the cache: command npm cache verify");
                console.log("  then fetch them deliberately, after review, in a real checkout: command npm ci");
            }
        ' "$1/package-lock.json"
    case "$out" in
        *"command false "*) echo "a git dependency was refused: git is disabled for this install (package.json or a lockfile names one)" ;;
    esac
    return "$status"
}

if check "sync test dependencies" "$UV" sync -q --extra test --python 3.13; then
    step "pytest" "$UV" run --no-sync --python 3.13 python -m pytest tests/ addons/ -q -p no:cacheprovider
fi

# The frontend job runs on every push, as tests.yml ran it on every PR.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
# addons/ too: add-on pages build, lint and test with the frontend (#9002).
git archive "$sha" dashboard addons | tar -x -C "$tmp"
frontend="$tmp/dashboard/frontend"
want="$(cat "$frontend/.nvmrc")"
have="$(node --version | sed 's/^v//; s/\..*//')"
if [ "$have" != "${want%%.*}" ]; then
    echo "WARNING: CI used Node $want (.nvmrc); this machine runs Node $have"
fi
if check "frontend dependencies (npm ci, offline, no scripts)" npm_ci_offline "$frontend"; then
    npm_in() { (cd "$frontend" && npm "$@"); }
    step "frontend lint" npm_in run lint
    step "frontend test" npm_in run test
    step "frontend build" npm_in run build
fi

if [ "${#failed[@]}" -gt 0 ]; then
    printf 'local checks FAILED: %s\n' "${failed[@]}"
    exit 1
fi
echo "local checks passed"
