"""pt worktrees: list and clean git worktrees."""

import subprocess
from pathlib import Path
from typing import Optional

import click
from commands.common import _caller_repo_root, console


@click.group(name="worktrees", invoke_without_command=True)
@click.pass_context
def worktrees_group(ctx):
    """Manage .claude/worktrees/ created by sub-agents."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(worktrees_list)


@worktrees_group.command(name="list")
def worktrees_list():
    """List all worktrees in .claude/worktrees/."""
    repo_root = _find_repo_root()
    if repo_root is None:
        console.print("[red]Not inside a git repository[/red]")
        return
    wt_dir = repo_root / ".claude" / "worktrees"
    if not wt_dir.exists():
        console.print("[dim]No .claude/worktrees/ directory found[/dim]")
        return
    entries = sorted(p for p in wt_dir.iterdir() if p.is_dir())
    if not entries:
        console.print("[dim]No worktrees found[/dim]")
        return
    # Get merged branches
    merged = _get_merged_branches(repo_root)
    console.print(f"\n[bold]Worktrees ({len(entries)})[/bold]\n")
    for entry in entries:
        branch = _worktree_branch(repo_root, entry)
        status = "[green]merged[/green]" if branch and branch in merged else "[yellow]active[/yellow]"
        branch_label = branch or "[dim]detached[/dim]"
        console.print(f"  {entry.name}  {branch_label}  {status}")
    console.print()


@worktrees_group.command(name="clean")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without removing")
@click.option("--force", is_flag=True, help="Also remove worktrees with uncommitted changes")
def worktrees_clean(dry_run, force):
    """Remove worktrees whose branches are merged to main or orphaned."""
    repo_root = _find_repo_root()
    if repo_root is None:
        console.print("[red]Not inside a git repository[/red]")
        return
    wt_dir = repo_root / ".claude" / "worktrees"
    if not wt_dir.exists():
        console.print("[dim]No .claude/worktrees/ directory found[/dim]")
        return
    entries = sorted(p for p in wt_dir.iterdir() if p.is_dir())
    if not entries:
        console.print("[dim]No worktrees to clean[/dim]")
        return

    merged = _get_merged_branches(repo_root)
    removed = []
    kept = []

    for entry in entries:
        branch = _worktree_branch(repo_root, entry)
        is_merged = branch and branch in merged
        is_orphaned = branch is None  # no branch ref found

        if is_merged or is_orphaned:
            reason = "merged" if is_merged else "orphaned"
            if dry_run:
                console.print(f"  [yellow]would remove[/yellow] {entry.name} ({reason}, branch: {branch or 'none'})")
                removed.append(entry.name)
            else:
                ok = _remove_worktree(repo_root, entry, force=force)
                if ok:
                    console.print(f"  [green]removed[/green] {entry.name} ({reason})")
                    removed.append(entry.name)
                    # Clean up the branch if it was a worktree-specific branch
                    if branch and branch.startswith("worktree-"):
                        _delete_local_branch(repo_root, branch)
                else:
                    console.print(f"  [red]failed to remove[/red] {entry.name} (use --force?)")
                    kept.append(entry.name)
        else:
            console.print(f"  [cyan]keeping[/cyan] {entry.name} (branch: {branch}, not merged)")
            kept.append(entry.name)

    prefix = "[bold yellow]Dry run:[/bold yellow] " if dry_run else ""
    console.print(f"\n{prefix}Removed {len(removed)}, kept {len(kept)}")


def _find_repo_root() -> Optional[Path]:
    """The git repo root of the directory the operator ran `pt` from.

    This used to try project-tracker's own root FIRST — `Path(__file__).parent
    .parent` — which always exists, so it returned project-tracker
    unconditionally no matter where `pt` was invoked. Its callers are
    `pt worktrees list` and `pt worktrees clean`, and clean REMOVES worktrees,
    so running it from another repo operated on project-tracker's worktrees
    instead. Same wrong-target family as #6851 (#6883).

    `Path.cwd()` is no help either: the launcher does `cd "$launcher_dir"`
    before exec'ing, so cwd inside pt.py is always project-tracker. The
    caller's real directory arrives as PT_CALLER_CWD.
    """
    root = _caller_repo_root()
    if (root / ".git").exists():
        return root
    # _caller_repo_root falls back to the caller cwd when it is not a repo;
    # walk up from there before giving up.
    cur = root
    while cur != cur.parent:
        if (cur / ".git").exists():
            return cur
        cur = cur.parent
    return None


def _get_merged_branches(repo_root: Path) -> set:
    """Return set of branch names that are merged into main."""
    try:
        result = subprocess.run(
            ["git", "branch", "--merged", "main"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
            check=True,
        )
        branches = set()
        for line in result.stdout.splitlines():
            name = line.strip().lstrip("* ").lstrip("+ ")
            if name and name != "main":
                branches.add(name)
        return branches
    except subprocess.CalledProcessError as e:  # governance: allow-silent SF002: CLI prints the warning; an empty set makes every worktree read as unmerged, so `pt worktrees clean` keeps them (fails safe)
        console.print(f"[yellow]Warning: Failed to check merged branches: {e.stderr.strip()}[/yellow]")
        return set()
    except subprocess.TimeoutExpired:  # governance: allow-silent SF002: CLI prints the warning; an empty set makes every worktree read as unmerged, so `pt worktrees clean` keeps them (fails safe)
        console.print("[yellow]Warning: Timed out checking merged branches[/yellow]")
        return set()


def _worktree_branch(repo_root: Path, wt_path: Path) -> Optional[str]:
    """Get the branch name for a worktree, or None if detached/missing.

    A git failure raises ClickException: `pt worktrees clean` treats None as
    "orphaned" and removes the worktree, so a failed lookup must never read
    as None.
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
            check=True,
        )
        # Parse porcelain output: blocks separated by blank lines
        current_path = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = line[len("worktree "):]
            elif line.startswith("branch ") and current_path:
                if Path(current_path).resolve() == wt_path.resolve():
                    # branch refs/heads/foo -> foo
                    ref = line[len("branch "):]
                    return ref.replace("refs/heads/", "")
        return None
    except subprocess.CalledProcessError as e:
        raise click.ClickException(
            f"Failed to determine branch for {wt_path.name}: {e.stderr.strip()}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise click.ClickException(
            f"Timed out determining branch for {wt_path.name}"
        ) from e


def _remove_worktree(repo_root: Path, wt_path: Path, force: bool = False) -> bool:
    """Remove a worktree using git worktree remove."""
    cmd = ["git", "worktree", "remove", str(wt_path)]
    if force:
        cmd.append("--force")
    try:
        result = subprocess.run(
            cmd, cwd=str(repo_root),
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            console.print(f"[yellow]Warning: git worktree remove failed for {wt_path.name}: {result.stderr.strip()}[/yellow]")
            return False
        return True
    except subprocess.TimeoutExpired:  # governance: allow-silent SF002: False is the "not removed" result; worktrees_clean prints "failed to remove" and keeps the entry
        console.print(f"[yellow]Warning: Timed out removing worktree {wt_path.name}[/yellow]")
        return False


def _delete_local_branch(repo_root: Path, branch: str) -> None:
    """Delete a local branch (merged only, safe -d flag)."""
    try:
        result = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            console.print(f"[yellow]Warning: Failed to delete branch {branch}: {result.stderr.strip()}[/yellow]")
    except subprocess.TimeoutExpired:
        console.print(f"[yellow]Warning: Timed out deleting branch {branch}[/yellow]")
