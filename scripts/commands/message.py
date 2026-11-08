"""pt message: send and read Agent Chat messages between agents and machines."""

import os
from pathlib import Path

import click
from db.manager import DatabaseManager

from scripts.config import chat_addresses


def _load_chat_config() -> dict:
    """Load Agent Chat config from ~/.claude/agent-chat.env or environment."""
    import os
    # governance: allow-silent SF003: empty means unset; ~/.claude/agent-chat.env may fill these below, and an empty url or key raises ClickException
    config = {
        "url": os.environ.get("AGENT_CHAT_URL", ""),
        "key": os.environ.get("AGENT_CHAT_API_KEY", ""),
        "sender": os.environ.get("AGENT_CHAT_SENDER", ""),
    }
    env_file = Path.home() / ".claude" / "agent-chat.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k == "AGENT_CHAT_URL":
                    config["url"] = v
                elif k == "AGENT_CHAT_API_KEY":
                    config["key"] = v
                elif k == "AGENT_CHAT_SENDER":
                    config["sender"] = v
    if not config["url"] or not config["key"]:
        raise click.ClickException(
            "Agent Chat not configured. Set AGENT_CHAT_URL and AGENT_CHAT_API_KEY "
            "in ~/.claude/agent-chat.env"
        )
    return config


def _chat_api_request(method: str, path: str, config: dict,
                      data: dict | None = None, params: dict | None = None) -> dict:
    """Make an HTTP request to the Agent Chat API. Returns parsed JSON."""
    import json as json_mod
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    url = config["url"].rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urlencode(filtered)

    payload = json_mod.dumps(data).encode("utf-8") if data else None
    headers = {"X-API-Key": config["key"]}
    if data:
        headers["Content-Type"] = "application/json"

    req = Request(url, data=payload, headers=headers, method=method)
    try:
        resp = urlopen(req, timeout=15)
        return json_mod.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise click.ClickException(f"Agent Chat API error {e.code}: {body}")
    except (URLError, OSError, TimeoutError) as e:
        raise click.ClickException(f"Agent Chat API unreachable: {e}")


def _known_chat_addresses() -> set | None:
    """Addresses a message can actually reach, or None when that can't be known.

    An address is a project name, so the set of deliverable addresses is the
    set of tracked projects, plus the agents and people listed under [chat]
    addresses in the local config. A recipient is known when its bare name is
    in the set (any machine) or its exact name@machine is. Returns None if the project list is
    unavailable — callers must treat that as "cannot verify", never as
    "invalid". An empty set is a real answer: nothing is reachable. A broken
    local config is an error, not "cannot verify".
    """
    try:
        known = chat_addresses()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        db = DatabaseManager()
        for project in db.get_all_projects():
            pid = (project.get("id") or "").strip()
            if pid:
                known.add(pid)
    # governance: allow-silent SF002: documented "cannot verify" value (see docstring); the send skips address verification rather than block on a DB hiccup, and the warning below says so
    except Exception as exc:  # noqa: BLE001 - never block a send on a DB hiccup
        click.echo(f"⚠ could not verify the recipient address (project list unavailable: {exc})", err=True)
        return None
    return known


def _chat_identity():
    """agent-chat/identity.py, the one rule for Agent Chat addresses (the chat hook uses it too)."""
    import sys as _sys
    agent_chat_root = str(Path(__file__).resolve().parents[2] / "agent-chat")
    if agent_chat_root not in _sys.path:
        _sys.path.insert(0, agent_chat_root)
    import identity
    return identity


def _qualify_chat_address(address: str, machine: str) -> str:
    """`my-app` + `mini` -> `my-app@mini`, ignoring an existing qualifier."""
    project, _ = _split_chat_address(address)
    return _chat_identity().qualify(project, machine)


def _self_machine_name() -> str:
    """This machine's @qualifier ("laptop", "mini"), per agent-chat/identity.py.

    The sender address is always BARE — `identity.project_name_for()` returns a
    basename and never a qualified address — so comparing a qualified recipient
    against the sender string can never tell you whether it names THIS machine.
    identity.machine_name() has to be consulted directly.
    """
    return _chat_identity().machine_name()


def _split_chat_address(address: str):
    """Split `my-app@mini` into ("my-app", "mini"); bare name -> (name, None)."""
    return _chat_identity().split_address(address)


def _resolve_self_address(config: dict | None = None) -> str:
    """This session's Agent Chat address, per agent-chat/identity.address_for.

    Taken from the directory pt was called from (PT_CALLER_CWD). Claude Code
    starts every Bash call in the session's project, so only an explicit
    `cd ../other && pt message send` speaks as another project. Outside any
    project, AGENT_CHAT_SENDER and then the configured sender are the fallbacks.
    """
    identity = _chat_identity()
    resolved = (identity.address_for(os.environ.get("PT_CALLER_CWD", os.getcwd()))
                or identity.configured_sender())
    if resolved:
        return resolved
    # governance: allow-silent SF003: empty means no address; `pt message send` refuses an empty sender with ClickException and `list` then shows the whole board, labelled as such
    fallback = (config or {}).get("sender", "") or os.environ.get("AGENT_CHAT_SENDER", "")
    # The configured sender is held to the same rule: no agent may speak as the human.
    return "" if identity.is_human(fallback) else fallback


def _format_message_line(m: dict) -> str:
    """Format a single message as a pipe-delimited line."""
    sender = m.get("sender", "?")
    recipient = m.get("recipient")
    arrow = f"{sender} -> {recipient}" if recipient else sender
    priority = m.get("priority", "normal")
    body_preview = (m.get("body") or "").replace("\n", " ")[:80]
    ts = m.get("ts", "")
    msg_id = m.get("id", "?")
    reply = f" (reply to #{m['reply_to']})" if m.get("reply_to") else ""
    return f"#{msg_id} | {ts} | {arrow}{reply} | {priority} | {body_preview}"


@click.group(name="message", invoke_without_command=True)
@click.pass_context
def message_group(ctx):
    """Agent Chat — send and read messages between agents.

    \b
    Quick start:
      pt message                           # Recent messages
      pt message send "Deploy looks good"  # Send to chat
      pt message list --limit 10           # List with filters
      pt message list --sender mini-claude # Filter by sender
      pt message send "Check status" --to mini-claude  # Direct message

    The sender is the project pt was run from. AGENT_CHAT_HUMAN, if set in
    your shell's environment (e.g. `export AGENT_CHAT_HUMAN=me` in ~/.zshenv),
    is the person's own address: no agent may send as it. Unset reserves nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    ctx.invoke(message_list)


@message_group.command(name="send")
@click.argument("text")
@click.option("--to", "recipient", default=None, help="Direct message to a specific agent")
@click.option("--priority", type=click.Choice(["low", "normal", "high", "urgent"]),
              default="normal", help="Message priority")
@click.option("--reply-to", "reply_to", type=int, default=None,
              help="Message ID to reply to")
@click.option("--machine", default=None,
              help="Target one machine specifically, e.g. --to my-app --machine mini")
@click.option("--force", is_flag=True,
              help="Send even if the address is not a known project")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def message_send(text, recipient, priority, reply_to, machine, force, json_output):
    """Send a message to Agent Chat.

    \b
    Examples:
      pt message send "SIW launch prep complete"
      pt message send "Check your inbox" --to mini-claude
      pt message send "Acknowledged" --reply-to 19 --priority high
    """
    import json as json_mod
    config = _load_chat_config()
    sender = _resolve_self_address(config)
    if not sender:
        raise click.ClickException(
            "No Agent Chat address for this session. Identity is resolved at "
            "session start from the project directory; if you are outside a "
            "project, set AGENT_CHAT_SENDER explicitly."
        )

    # Sending to yourself is almost always a mis-addressed message, and it used
    # to succeed silently: every agent shared one machine-global identity, so
    # `--to claude-architect` returned "Sent #92 as claude-architect".
    if machine:
        if not recipient:
            raise click.ClickException("--machine needs --to; it qualifies an address.")
        recipient = _qualify_chat_address(recipient, machine)

    if recipient:
        target_project, target_machine = _split_chat_address(recipient)
        self_project, _ = _split_chat_address(sender)
        # The `recipient == sender` conjunct this used to carry was redundant
        # for a bare address (already implied by the project comparison) and
        # actively harmful for a qualified one: the sender is ALWAYS bare, so
        # `--to project-tracker --machine laptop` rewrote the recipient to
        # `project-tracker@laptop`, which never equals bare `project-tracker`,
        # the guard fell through, and the message went to this very session
        # (#6775). Compare machines instead. `project-tracker@mini` from a
        # laptop session stays allowed — that is a different machine's floor
        # manager, a real peer.
        self_machine = _self_machine_name()
        names_this_machine = (
            target_machine is None or target_machine == self_machine
        )
        if target_project == self_project and names_this_machine:
            raise click.ClickException(
                f"'{recipient}' is this session's own address — that message would "
                f"go nowhere. Use --to <other-project>, or omit --to to broadcast."
            )

        # An unknown address is accepted by the server and delivered to nobody.
        # That is how message #91 was lost: addressed to a name no session
        # listens on, reported as sent. Verify before claiming success.
        known = None if force else _known_chat_addresses()
        if known is not None and target_project not in known and recipient not in known:
            import difflib
            close = difflib.get_close_matches(target_project, sorted(known), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            raise click.ClickException(
                f"'{recipient}' is not a known address, so this message would be "
                f"accepted and delivered to nobody.{hint}\n"
                f"Addresses are project names. Use --force to send anyway."
            )

    payload = {
        "sender": sender,
        "body": text,
        "priority": priority,
    }
    if recipient:
        payload["to"] = recipient
    if reply_to:
        payload["reply_to"] = reply_to

    result = _chat_api_request("POST", "/send", config, data=payload)

    if json_output:
        print(json_mod.dumps(result, indent=2))
    else:
        target = f" to {recipient}" if recipient else " (broadcast)"
        click.echo(f"Sent #{result.get('id', '?')} as {sender}{target}")


@message_group.command(name="list")
@click.option("--limit", "-n", default=20, type=int, help="Number of messages (default: 20)")
@click.option("--since", default=None, help="Show messages after this ISO timestamp")
@click.option("--sender", default=None, help="Filter by sender")
@click.option("--for", "for_recipient", default=None,
              help="Show messages visible to this agent (broadcasts + DMs). "
                   "Defaults to your own address; use --all for the full board.")
@click.option("--all", "show_all", is_flag=True,
              help="Show all traffic, not just your inbox")
@click.option("--oldest-first", is_flag=True,
              help="Oldest first (default is newest first)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def message_list(limit, since, sender, for_recipient, show_all, oldest_first, json_output):
    """List messages from Agent Chat.

    Defaults to INBOX semantics: your own broadcasts + DMs, newest first.

    \b
    Examples:
      pt message list                        # Your inbox, newest 20
      pt message list --all                  # Everyone's traffic
      pt message list --limit 5              # Newest 5 in your inbox
      pt message list --sender mini-claude   # From mini-claude
      pt message list --for my-app           # Someone else's inbox
      pt message list --since 2026-04-03T00:00:00Z
    """
    import json as json_mod
    config = _load_chat_config()

    # Inbox by default. Without this the default view is the whole board, and
    # a DM addressed to you is indistinguishable from noise.
    if for_recipient is None and not show_all:
        for_recipient = _resolve_self_address(config)

    def _fetch(fetch_limit, order=None):
        params = {"limit": str(fetch_limit)}
        if order:
            params["order"] = order
        if since:
            params["since"] = since
        if sender:
            params["sender"] = sender
        if for_recipient:
            params["for"] = for_recipient
        return _chat_api_request("GET", "/messages", config, params=params)

    # `order=desc` makes the server select the NEWEST rows. Ordering is not
    # cosmetic here: with ASC, `--limit 20` returns the twenty OLDEST messages,
    # which is why the default view sat on 2026-04-02 while #91 arrived in
    # August. Reversing the page client-side does not fix that — it reorders
    # the wrong rows.
    result = _fetch(limit, order="desc")
    messages = result.get("messages", [])

    # Older deployments ignore `order` and still return the oldest page. Detect
    # that (a full page whose newest row is not the board's newest) and recover
    # by pulling a wider window and taking the tail. One extra call, and only
    # against a server that has not been redeployed yet.
    if result.get("order") != "desc" and len(messages) >= limit:
        wide = _fetch(max(limit * 10, 500))
        wide_messages = wide.get("messages", [])
        if len(wide_messages) > len(messages):
            messages = wide_messages[-limit:]
            result = dict(wide, messages=messages)

    if not oldest_first:
        messages = list(reversed(messages))
    result = dict(result, messages=messages)

    if json_output:
        print(json_mod.dumps(result, indent=2))
        return
    if not messages:
        scope = "your inbox" if for_recipient and not show_all else "the board"
        click.echo(f"No messages found in {scope}.")
        return
    for m in messages:
        click.echo(_format_message_line(m))
    click.echo(f"\nMessages: {len(messages)}")
