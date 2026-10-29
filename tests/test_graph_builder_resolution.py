"""Import and name resolution in the project graph stays inside a project.

Each project under ~/projects is its own repository, so an import or a bare
file name can only mean a file in the importing project. The resolvers used
to search the whole portfolio and take the first hit, which turned stdlib and
npm imports into edges to unrelated files elsewhere (``import re`` ->
``beta/store.py``) and dragged large projects into the middle of /graph.
"""

from pathlib import Path

import pytest

from scripts.discovery.graph_builder import GraphBuilder


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _build(tmp_path: Path) -> GraphBuilder:
    # alpha: the importing project
    _write(tmp_path, "alpha/app.py",
           "import re\nimport logging\nimport types\n"
           "from utils import helper\nfrom scripts.config import X\n"
           "from scripts import (\n    tools,\n    extra as e,\n)\nfrom .sibling import y\n")
    _write(tmp_path, "alpha/utils.py")
    _write(tmp_path, "alpha/sibling.py")
    _write(tmp_path, "alpha/scripts/config.py")
    _write(tmp_path, "alpha/scripts/tools.py")
    _write(tmp_path, "alpha/scripts/extra.py")
    _write(tmp_path, "alpha/web/page.tsx",
           "import React from 'react'\nimport next from 'next'\n"
           "import { T } from './types'\nimport api from '../api'\n"
           "import { h } from './hooks.js'\nimport C from './components'\n")
    _write(tmp_path, "alpha/web/types.ts")
    _write(tmp_path, "alpha/web/hooks.ts")
    _write(tmp_path, "alpha/web/components/index.tsx")
    _write(tmp_path, "alpha/api.ts")
    _write(tmp_path, "alpha/main.go", 'import "example.com/alpha/server"\n')
    _write(tmp_path, "alpha/server/server.go")
    _write(tmp_path, "alpha/Widget.swift", "struct Widget {}\n")
    _write(tmp_path, "alpha/Uses.swift", "let w = Widget()\n")
    _write(tmp_path, "alpha/notes.md", "[[design]]\n# See: helpers.py\n")
    _write(tmp_path, "alpha/design.md")

    # beta: bait that the old portfolio-wide matching picked up
    _write(tmp_path, "beta/store.py")          # endswith "re.py"
    _write(tmp_path, "beta/logging.py")        # stdlib name
    _write(tmp_path, "beta/types.py")          # stdlib name
    _write(tmp_path, "beta/utils.py")          # same module name as alpha's
    _write(tmp_path, "beta/reactive.md")       # contains "react"
    _write(tmp_path, "beta/nextsteps.md")      # contains "next"
    _write(tmp_path, "beta/types.ts")          # same file name as alpha's
    _write(tmp_path, "beta/server.go")
    _write(tmp_path, "beta/helpers.py")
    _write(tmp_path, "beta/design.md")
    _write(tmp_path, "beta/UsesToo.swift", "let w = Widget()\n")

    builder = GraphBuilder(tmp_path)
    builder.scan()
    return builder


def _file_edges(builder: GraphBuilder) -> set[tuple[str, str]]:
    return {
        (e["source"], e["target"])
        for e in builder.edges
        if e["type"] != "project_member"
    }


def test_no_edge_crosses_a_project_boundary(tmp_path):
    builder = _build(tmp_path)
    project = {n["id"]: n["project"] for n in builder.nodes}

    crossing = [
        (e["source"], e["target"], e["label"])
        for e in builder.edges
        if project[e["source"]] != project[e["target"]]
    ]

    assert crossing == []


def test_genuine_same_project_references_still_resolve(tmp_path):
    edges = _file_edges(_build(tmp_path))

    assert edges == {
        ("alpha/app.py", "alpha/utils.py"),
        ("alpha/app.py", "alpha/scripts/config.py"),
        ("alpha/app.py", "alpha/scripts/tools.py"),
        ("alpha/app.py", "alpha/scripts/extra.py"),
        ("alpha/app.py", "alpha/sibling.py"),
        ("alpha/web/page.tsx", "alpha/web/types.ts"),
        ("alpha/web/page.tsx", "alpha/api.ts"),
        ("alpha/web/page.tsx", "alpha/web/hooks.ts"),
        ("alpha/web/page.tsx", "alpha/web/components/index.tsx"),
        ("alpha/main.go", "alpha/server/server.go"),
        ("alpha/Uses.swift", "alpha/Widget.swift"),
        ("alpha/notes.md", "alpha/design.md"),
    }


def test_python_shadowing_relative_levels_and_ambiguity(tmp_path):
    # A stdlib name is the stdlib even with a same-named file beside the
    # importer: inside a package Python 3 never loads the sibling.
    _write(tmp_path, "gamma/tools/run.py",
           "import queue\nimport logging\nfrom ..core.base import B\nimport helpers\n")
    _write(tmp_path, "gamma/tools/queue.py")
    _write(tmp_path, "gamma/lib/logging.py")
    _write(tmp_path, "gamma/core/base.py")
    # two modules named helpers: the importer's sibling wins over the shallower one
    _write(tmp_path, "gamma/helpers.py")
    _write(tmp_path, "gamma/tools/helpers.py")
    _write(tmp_path, "gamma/deep/a/b/c.py", "import helpers\n")

    builder = GraphBuilder(tmp_path)
    builder.scan()

    assert _file_edges(builder) == {
        ("gamma/tools/run.py", "gamma/core/base.py"),
        ("gamma/tools/run.py", "gamma/tools/helpers.py"),
        ("gamma/deep/a/b/c.py", "gamma/helpers.py"),
    }


def test_project_qualified_python_imports_resolve(tmp_path):
    # A project directory can itself be the importable package
    _write(tmp_path, "alpha/use.py", "import alpha.foo\nfrom alpha import bar\n")
    _write(tmp_path, "alpha/foo.py")
    _write(tmp_path, "alpha/bar.py")

    builder = GraphBuilder(tmp_path)
    builder.scan()

    assert _file_edges(builder) == {
        ("alpha/use.py", "alpha/foo.py"),
        ("alpha/use.py", "alpha/bar.py"),
    }


def test_the_local_configs_graph_lists_reach_the_scan(tmp_path, monkeypatch):
    """[graph] skip_dirs drops a directory everywhere; ignore_projects drops a whole project."""
    root = tmp_path / "projects"
    for path in ("keep/app.py", "keep/scratch/note.md", "skipme/app.py", "other/app.py"):
        _write(root, path)
    (tmp_path / "config.toml").write_text('[graph]\nskip_dirs = ["scratch"]\nignore_projects = ["skipme"]\n')
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))
    builder = GraphBuilder(root)
    builder.scan()
    assert sorted(node["path"] for node in builder.nodes) == ["keep/app.py", "other/app.py"]


def test_a_broken_graph_section_fails_the_scan_naming_the_file(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[graph]\nignore_projects = "skipme"\n')
    monkeypatch.setenv("PT_CONFIG", str(tmp_path / "config.toml"))
    with pytest.raises(ValueError, match=r"config\.toml: \[graph\]"):
        GraphBuilder(tmp_path).scan()
