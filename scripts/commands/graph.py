"""pt graph: wraps the memory store's brain.py graph subcommands."""

import os

import click
from commands.memory import _run_brain


@click.group(name="graph", invoke_without_command=True)
@click.pass_context
def graph_group(ctx: click.Context) -> None:
    """Query and manage the memory store's knowledge graph.

    The store extracts structured nodes (projects, people, artifacts,
    concepts, tasks, decisions) and edges (co_mentioned, belongs_to, relates_to)
    from all memories in brain.db.

    \b
    Node types:  project, person, artifact, concept, task, decision, tag
    Edge types:  co_mentioned, belongs_to, involved_in, tracked_in, relates_to

    \b
    Quick start:
      pt graph stats                          # Node/edge/confidence counts
      pt graph query project-tracker          # What's connected to a project
      pt graph find "backup strategy"         # Semantic search over nodes
      pt graph path project-tracker Doppler  # Shortest path between nodes
      pt graph build                          # Rebuild from all memories
    """
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@graph_group.command(name="stats")
def graph_stats() -> None:
    """Show memory graph statistics — node/edge counts by type.

    \b
    Example output:
      Nodes: 1,498 (971 artifacts, 228 tasks, 193 concepts, ...)
      Edges: 11,225 (co_mentioned, belongs_to, relates_to, ...)
    """
    _run_brain("graph", "stats")


@graph_group.command(name="query")
@click.argument("project", required=False, default="")
@click.option("--hops", "-h", default=1, type=int,
              help="Degrees of separation from project node (default: 1)")
@click.option("--limit", "-n", default=20, type=int,
              help="Max nodes to return (default: 20)")
@click.option("--types", "-t", default="",
              help="Comma-separated node types to filter (e.g. decision,person)")
@click.option("--since", "-s", default="",
              help="Only nodes updated on/after this date (e.g. 2026-04-04)")
@click.option("--json", "as_json", is_flag=True,
              help="Output as JSON for agent consumption")
def graph_query(project: str, hops: int, limit: int, types: str, since: str, as_json: bool) -> None:
    """Query nodes connected to a project.

    Returns people, files, decisions, concepts, and tasks linked to the
    given project within N hops. Project auto-detected from cwd if omitted.
    Nodes ranked by freshness (mention count * temporal decay, 14-day half-life).

    \b
    Examples:
      pt graph query                              # Auto-detect from cwd
      pt graph query project-tracker              # Direct connections
      pt graph query project-tracker --hops 2         # Two degrees out
      pt graph query my-app --types decision,person  # Filter by type
      pt graph query my-app --since 2026-04-04       # What's new?
      pt graph query my-app --json                # JSON for agents
    """
    if not project:
        project = os.path.basename(os.getcwd())
    args = ["graph", "query", project, "--hops", str(hops), "--limit", str(limit)]
    if types:
        args.extend(["--types", types])
    if since:
        args.extend(["--since", since])
    if as_json:
        args.append("--json")
    _run_brain(*args)


@graph_group.command(name="find")
@click.argument("query")
@click.option("--top", "-k", default=10, type=int,
              help="Number of top matches (default: 10)")
@click.option("--hops", "-h", default=1, type=int,
              help="Expand to N-hop neighbors (0 = exact matches only)")
@click.option("--json", "as_json", is_flag=True,
              help="Output as JSON for agent consumption")
def graph_find(query: str, top: int, hops: int, as_json: bool) -> None:
    """Semantic search over graph nodes.

    Finds the graph nodes most similar to your query using embeddings,
    then expands to their neighbors. The 'what does the graph know about X?' query.

    \b
    Examples:
      pt graph find "authentication"              # What's related to auth?
      pt graph find "backup strategy" --top 5     # Focused search
      pt graph find "LoRA training" --hops 0      # Exact matches only
      pt graph find "deployment" --json            # JSON for agents
    """
    args = ["graph", "find", query, "--top", str(top), "--hops", str(hops)]
    if as_json:
        args.append("--json")
    _run_brain(*args)


@graph_group.command(name="path")
@click.argument("source")
@click.argument("target")
@click.option("--max-depth", "-d", default=5, type=int,
              help="Max BFS depth (default: 5)")
@click.option("--json", "as_json", is_flag=True,
              help="Output as JSON for agent consumption")
def graph_path(source: str, target: str, max_depth: int, as_json: bool) -> None:
    """Find shortest path between two nodes.

    BFS traversal from source to target. Shows the chain of nodes and
    edge types connecting them. Useful for understanding how projects relate.

    \b
    Examples:
      pt graph path my-app project-tracker        # How are they connected?
      pt graph path my-app other-app             # Cross-project links
      pt graph path my-app Doppler --json         # JSON for agents
    """
    args = ["graph", "path", source, target, "--max-depth", str(max_depth)]
    if as_json:
        args.append("--json")
    _run_brain(*args)


@graph_group.command(name="export")
def graph_export() -> None:
    """Export the full memory graph as JSON (nodes + edges).

    Outputs to stdout. Pipe to a file or tool:

    \b
    Examples:
      pt graph export                    # Print to terminal
      pt graph export > graph.json       # Save to file
      pt graph export | jq '.nodes | length'  # Count nodes
    """
    _run_brain("graph", "export")


@graph_group.command(name="build")
@click.option("--force", is_flag=True, help="Truncate existing graph before rebuild")
def graph_build(force: bool) -> None:
    """Rebuild the memory graph from all memories.

    Scans every entry in brain.db, extracts entities (projects, people,
    files, concepts, tasks, decisions), builds edges from co-mentions
    and semantic similarity, and writes to graph_nodes/graph_edges tables.

    \b
    Examples:
      pt graph build           # Incremental update
      pt graph build --force   # Full rebuild from scratch
    """
    args = ["graph", "build"]
    if force:
        args.append("--force")
    _run_brain(*args)
