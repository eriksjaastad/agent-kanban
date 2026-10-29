"""Agent Registry for the Agent Dispatcher UI."""
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from scripts.config import PROJECTS_BASE_DIR

# Configuration
PROJECTS_ROOT = str(PROJECTS_BASE_DIR)


@dataclass
class AgentCommand:
    """A command that an agent can execute."""
    name: str
    description: str
    args_template: str = ""
    dangerous: bool = False


@dataclass
class Agent:
    """An agent that can be triggered from the dashboard."""
    name: str
    description: str
    binary_path: str
    commands: list[AgentCommand] = field(default_factory=list)
    available: bool = True


# Agent registry
AGENTS: dict[str, Agent] = {}


def _init_agents():
    """Populate the agent registry."""
    global AGENTS

    # pt (project-tracker)
    pt_path = str(Path(__file__).parent.parent.parent / "pt")
    AGENTS["pt"] = Agent(
        name="pt",
        description="Project Tracker CLI",
        binary_path=pt_path,
        available=Path(pt_path).exists(),
        commands=[
            AgentCommand("scan", "Full project scan", ""),
            AgentCommand("list", "List tracked projects", ""),
            AgentCommand("message", "Agent Chat - send and read messages", ""),
        ]
    )


# Initialize on import
_init_agents()


def get_available_agents() -> list[Agent]:
    """Return list of all registered agents."""
    return list(AGENTS.values())


def get_agent(name: str) -> Optional[Agent]:
    """Get agent by name, or None if not found."""
    return AGENTS.get(name)


def get_agent_command(agent_name: str, command_name: str) -> Optional[AgentCommand]:
    """Get specific command from an agent."""
    agent = get_agent(agent_name)
    if not agent:
        return None
    for cmd in agent.commands:
        if cmd.name == command_name:
            return cmd
    return None


@dataclass
class CommandResult:
    """Result of running an agent command."""
    success: bool
    output: str
    error: str
    return_code: int
    duration_ms: int
    command: str


def run_agent_command(
    agent_name: str,
    command_name: str,
    args: str = ""
) -> CommandResult:
    """
    Execute an agent command and capture output.

    Args:
        agent_name: Name of the agent (e.g., "pt")
        command_name: Name of the command (e.g., "health")
        args: Additional arguments as string

    Returns:
        CommandResult with output and status
    """
    agent = get_agent(agent_name)
    if not agent:
        return CommandResult(
            success=False,
            output="",
            error=f"Agent not found: {agent_name}",
            return_code=-1,
            duration_ms=0,
            command=""
        )

    if not agent.available:
        return CommandResult(
            success=False,
            output="",
            error=f"Agent binary not found: {agent.binary_path}",
            return_code=-1,
            duration_ms=0,
            command=""
        )

    command = get_agent_command(agent_name, command_name)
    if not command:
        return CommandResult(
            success=False,
            output="",
            error=f"Command not found: {command_name}",
            return_code=-1,
            duration_ms=0,
            command=""
        )

    # Build command line
    cmd_parts = [agent.binary_path, command_name]
    if args:
        cmd_parts.extend(shlex.split(args))

    full_command = " ".join(cmd_parts)

    start_time = time.time()
    try:
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=PROJECTS_ROOT
        )
        duration_ms = int((time.time() - start_time) * 1000)

        return CommandResult(
            success=result.returncode == 0,
            output=result.stdout,
            error=result.stderr,
            return_code=result.returncode,
            duration_ms=duration_ms,
            command=full_command
        )

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start_time) * 1000)
        return CommandResult(
            success=False,
            output="",
            error="Command timed out after 60 seconds",
            return_code=-1,
            duration_ms=duration_ms,
            command=full_command
        )
    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        return CommandResult(
            success=False,
            output="",
            error=str(e),
            return_code=-1,
            duration_ms=duration_ms,
            command=full_command
        )
