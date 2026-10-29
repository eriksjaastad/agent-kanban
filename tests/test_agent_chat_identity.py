"""Tests for Agent Chat identity resolution (#6772).

Agent Chat silently dropped every direct message from 2026-07-23 to
2026-08-30. Two causes, and these tests cover the identity half:

  - Every agent on a machine shared AGENT_CHAT_SENDER=claude-architect, so
    `--to claude-architect` sent to itself and DMs could not address a
    specific floor manager.
  - A cwd-derived address would have replaced that bug with a worse one: an
    agent that cd's into a peer's repo would assume the peer's identity.

The ruling: ADDRESS = project name, taken from where the agent was
started, never from where it has cd'd since (see identity.py).
"""

import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-chat"))

import identity  # noqa: E402


@pytest.fixture
def projects(tmp_path, monkeypatch):
    """A fake ~/projects with two project dirs and a non-project dir."""
    root = tmp_path / "projects"
    (root / "project-tracker").mkdir(parents=True)
    (root / "project-tracker" / ".git").mkdir()
    (root / "notes").mkdir()
    (root / "notes" / ".git").mkdir()
    (root / "notes-no-marker").mkdir()
    monkeypatch.setenv("PROJECTS_ROOT", str(root))
    monkeypatch.setenv("AGENT_CHAT_HUMAN", "lead")
    monkeypatch.delenv("AGENT_CHAT_SENDER", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    return root


class TestProjectNameResolution:
    """Address comes from the project NAME, never the path."""

    def test_project_root_resolves_to_its_name(self, projects):
        assert identity.project_name_for(projects / "notes") == "notes"

    def test_subdirectory_resolves_to_the_project_not_the_subdir(self, projects):
        """cwd deep inside a project still yields the project address."""
        deep = projects / "project-tracker" / "dashboard" / "frontend" / "src"
        deep.mkdir(parents=True)
        assert identity.project_name_for(deep) == "project-tracker"

    def test_projects_root_is_the_architect(self, projects):
        assert identity.project_name_for(projects) == identity.ARCHITECT_ADDRESS

    def test_outside_any_project_returns_none(self, projects, tmp_path):
        """No address is better than a wrong one."""
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        assert identity.project_name_for(outside) is None

    def test_path_layout_does_not_affect_the_address(self, tmp_path, monkeypatch):
        """The Mini nests projects differently; the address must not care."""
        alt = tmp_path / "different" / "layout"
        (alt / "notes" / ".git").mkdir(parents=True)
        monkeypatch.setenv("PROJECTS_ROOT", str(alt))
        assert identity.project_name_for(alt / "notes") == "notes"

    def test_human_address_is_reserved(self, projects):
        """An agent must not be able to resolve to the human's address."""
        (projects / "lead" / ".git").mkdir(parents=True)
        assert identity.project_name_for(projects / "lead") is None

    def test_claude_md_marks_a_project_without_git(self, projects):
        proj = projects / "no-git-project"
        proj.mkdir()
        (proj / "CLAUDE.md").write_text("# project")
        assert identity.project_name_for(proj) == "no-git-project"


class TestUnderscorePrefixedAddress:
    """#7798 — `_shared` must bind. A leading underscore is a real naming
    convention for some projects, and the server already accepts `_shared` as
    an address; only the client-side grammar rejected it.
    """

    def test_leading_underscore_project_resolves_to_its_name(self, projects):
        proj = projects / "_shared"
        (proj / ".git").mkdir(parents=True)
        assert identity.project_name_for(proj) == "_shared"

    def test_leading_underscore_project_resolves_from_a_subdirectory(self, projects):
        proj = projects / "_shared"
        deep = proj / "scripts"
        deep.mkdir(parents=True)
        (proj / ".git").mkdir()
        assert identity.project_name_for(deep) == "_shared"

    def test_all_underscore_name_is_rejected(self, projects):
        """An address needs at least one alnum character; bare underscores
        are not a sensible project name and are treated as invalid, the same
        way an all-symbol name always was.
        """
        proj = projects / "___"
        (proj / ".git").mkdir(parents=True)
        assert identity.project_name_for(proj) is None

    def test_leading_dot_is_still_invalid(self, projects):
        """Hidden dirs are not projects — unchanged by the underscore fix."""
        proj = projects / ".claude"
        (proj / ".git").mkdir(parents=True)
        assert identity.project_name_for(proj) is None

    def test_underscore_prefixed_project_is_an_address(self, projects):
        proj = projects / "_shared"
        (proj / ".git").mkdir(parents=True)
        assert identity.address_for(proj) == "_shared"


class TestAddressFor:
    """The address of an agent started in a directory."""

    def test_project_and_its_subdirectories_share_one_address(self, projects):
        (projects / "notes" / "src").mkdir()
        assert identity.address_for(projects / "notes") == "notes"
        assert identity.address_for(projects / "notes" / "src") == "notes"

    def test_projects_root_is_the_architect(self, projects):
        assert identity.address_for(projects) == "claude-architect"

    def test_outside_a_project_has_no_address(self, projects, tmp_path):
        (tmp_path / "nowhere").mkdir()
        assert identity.address_for(tmp_path / "nowhere") is None


class TestMachineQualifier:
    """`notes@mini` addresses one machine; bare `notes` addresses the project."""

    def test_qualify_appends_machine(self):
        assert identity.qualify("notes", "mini") == "notes@mini"

    def test_qualify_without_machine_is_bare(self):
        assert identity.qualify("notes", None) == "notes"

    def test_split_qualified_address(self):
        assert identity.split_address("notes@mini") == ("notes", "mini")

    def test_split_bare_address_means_any_machine(self):
        assert identity.split_address("notes") == ("notes", None)

    def test_machine_name_is_overridable(self, monkeypatch):
        monkeypatch.setenv("AGENT_CHAT_MACHINE", "MINI")
        assert identity.machine_name() == "mini"

    def test_machine_name_derived_from_hostname(self, monkeypatch):
        monkeypatch.delenv("AGENT_CHAT_MACHINE", raising=False)
        monkeypatch.setattr(identity.socket, "gethostname", lambda: "Office-Mac-mini.lan")
        assert identity.machine_name() == "mini"

    def test_unknown_hostname_still_yields_a_usable_qualifier(self, monkeypatch):
        monkeypatch.delenv("AGENT_CHAT_MACHINE", raising=False)
        monkeypatch.setattr(identity.socket, "gethostname", lambda: "Some Box.lan")
        name = identity.machine_name()
        assert name and " " not in name


class TestLegacyCorpusMapping:
    """Existing addresses must map onto the new scheme, not break."""

    def test_existing_addresses_split_cleanly(self):
        for addr in ("notes", "project-tracker", "claude-architect"):
            project, machine = identity.split_address(addr)
            assert project == addr and machine is None

    def test_mini_claude_becomes_machine_qualified(self):
        project, machine = identity.split_address("studio@mini")
        assert (project, machine) == ("studio", "mini")


class TestReservedHumanAddressEnforcement:
    """The human's address must be unreachable by an agent on EVERY path, not just one.

    Enforcing it only in project_name_for() left AGENT_CHAT_SENDER=<the human> as an
    unguarded route — and that env fallback is what every session uses before
    the hooks are installed.
    """

    @pytest.mark.parametrize("sender", ["lead", "lead@mini", "lead+codex", ""])
    def test_configured_sender_cannot_claim_the_human_address(self, projects, monkeypatch, sender):
        monkeypatch.setenv("AGENT_CHAT_SENDER", sender)
        assert identity.configured_sender() is None

    def test_other_configured_senders_still_work(self, projects, monkeypatch):
        monkeypatch.setenv("AGENT_CHAT_SENDER", " claude-architect ")
        assert identity.configured_sender() == "claude-architect"


class TestAgentQualifier:
    """#7146 — concurrent agents in one directory need distinct addresses."""

    def test_agent_env_qualifies_project_address(self, projects, monkeypatch):
        monkeypatch.setenv("AGENT_CHAT_AGENT", "hermes")
        assert identity.address_for(projects / "notes") == "notes+hermes"

    def test_two_agents_same_cwd_get_distinct_addresses(self, projects, monkeypatch):
        monkeypatch.setenv("AGENT_CHAT_AGENT", "claude")
        a = identity.address_for(projects / "notes")
        monkeypatch.setenv("AGENT_CHAT_AGENT", "hermes")
        b = identity.address_for(projects / "notes")
        assert a == "notes+claude"
        assert b == "notes+hermes"
        assert a != b

    def test_architect_with_agent_qualifier(self, projects, monkeypatch):
        monkeypatch.setenv("AGENT_CHAT_AGENT", "codex")
        assert identity.address_for(projects) == "claude-architect+codex"

    def test_legacy_without_agent_env_stays_bare(self, projects, monkeypatch):
        monkeypatch.delenv("AGENT_CHAT_AGENT", raising=False)
        assert identity.address_for(projects / "notes") == "notes"

    def test_split_local_peels_agent(self):
        assert identity.split_local("notes+hermes") == ("notes", "hermes")
        assert identity.split_local("notes") == ("notes", None)

    def test_invalid_agent_env_ignored(self, projects, monkeypatch):
        monkeypatch.setenv("AGENT_CHAT_AGENT", "bad agent!")
        assert identity.address_for(projects / "notes") == "notes"


class TestIdentityFailuresAreVisible:
    """#6900: a directory that can't be resolved is a failure, never "outside a project"."""

    def test_unresolvable_cwd_raises_instead_of_outside_project(self, projects, monkeypatch):
        def broken_resolve(self, strict=False):
            raise OSError("resolve failed")

        monkeypatch.setattr(identity.Path, "resolve", broken_resolve)
        with pytest.raises(OSError):
            identity.find_project_root(projects / "notes")


def test_without_a_configured_human_no_name_is_reserved(projects, tmp_path, monkeypatch):
    """AGENT_CHAT_HUMAN is optional: unset, a project named like a person is just a project."""
    monkeypatch.delenv("AGENT_CHAT_HUMAN")
    monkeypatch.setenv("HOME", str(tmp_path))  # no ~/.claude/agent-chat.env
    (projects / "lead" / ".git").mkdir(parents=True)
    monkeypatch.setenv("AGENT_CHAT_SENDER", "lead")
    assert (identity.human_address(), identity.project_name_for(projects / "lead"),
            identity.configured_sender()) == (None, "lead", "lead")


def test_an_agent_slug_cannot_be_the_human_address(projects, monkeypatch):
    """AGENT_CHAT_AGENT=<the human> is ignored, so the address stays the bare project."""
    monkeypatch.setenv("AGENT_CHAT_AGENT", "lead")
    assert (identity.agent_name(), identity.address_for(projects / "notes")) == (None, "notes")


@pytest.mark.parametrize("value", ["lead@mini", "lead+codex"])
def test_a_qualified_human_address_is_refused(monkeypatch, value):
    monkeypatch.setenv("AGENT_CHAT_HUMAN", value)
    with pytest.raises(ValueError, match="AGENT_CHAT_HUMAN"):
        identity.human_address()


def test_the_architect_address_can_be_the_reserved_human(projects, monkeypatch):
    monkeypatch.setenv("AGENT_CHAT_HUMAN", identity.ARCHITECT_ADDRESS)
    monkeypatch.setenv("AGENT_CHAT_AGENT", "codex")
    assert (identity.project_name_for(projects), identity.address_for(projects)) == (None, None)


@pytest.mark.parametrize("variable", ["AGENT_CHAT_AGENT", "AGENT_CHAT_AGENT_NAME"])
@pytest.mark.parametrize("human, slug, kept", [("Lead", "Lead", None), ("lead", "LEAD", None), ("Lead", "codex", "codex")])
def test_an_agent_slug_is_compared_with_the_human_case_blind(projects, monkeypatch, variable, human, slug, kept):
    monkeypatch.setenv("AGENT_CHAT_HUMAN", human)
    monkeypatch.delenv("AGENT_CHAT_AGENT", raising=False)
    monkeypatch.setenv(variable, slug)
    assert identity.agent_name() == kept


@settings(max_examples=300, deadline=None)
@given(value=st.one_of(st.none(), st.text("abL0._-;&|<>()$`\\ '\"#@+", max_size=6)))
@example(value=None)
@example(value="")
@example(value=" lead ")
@example(value="lead;")
@example(value="lead@mini")
def test_the_human_is_a_plain_name_from_the_environment(value):
    """Read from AGENT_CHAT_HUMAN only: unset or empty is no human, a plain name is the human,
    anything else raises without repeating the value."""
    with patch.dict(os.environ):
        os.environ.pop("AGENT_CHAT_HUMAN", None)
        if value is not None:
            os.environ["AGENT_CHAT_HUMAN"] = value
        name = (value or "").strip()
        if re.fullmatch(r"[A-Za-z0-9._-]*", name):
            assert identity.human_address() == (name or None)
        else:
            with pytest.raises(ValueError) as error:
                identity.human_address()
            # One fixed message, so the value can never leak into it.
            assert str(error.value) == "AGENT_CHAT_HUMAN must be a bare name: letters, digits, '.', '_', '-'"
