#!/usr/bin/env -S uv run --no-project --python 3.13 python
"""Report which build of agent-chat is actually serving traffic.

`gcloud run revisions list` is the obvious way to ask this, and it is the wrong
one: gcloud auth on the deploying machine expires constantly, and it has already
blocked two separate sessions from answering the question. This service sat five
months behind main partly because "what is deployed?" was expensive to ask. So
the answer comes over plain HTTP, from the same /health endpoint the container's
own healthcheck uses.

Reads the health JSON on stdin so the caller owns the network call:

    curl -sS --max-time 10 "$URL/health" | scripts/agent_chat_status.py

Never raises on bad input. A status command that tracebacks when the service is
down is useless exactly when it is needed -- the service being unreachable is a
*result*, not an error, and it exits 0 so it can be used in a pipeline.
"""

import json
import sys


def describe(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return "agent-chat UNREACHABLE: empty response (service down, or curl failed)"
    try:
        parsed = json.loads(raw)
    except ValueError:
        # Covers a truncated body too: --max-time can cut a response mid-object,
        # which yields a '{'-prefixed string that is not valid JSON.
        return f"agent-chat returned non-JSON: {raw[:200]}"
    if not isinstance(parsed, dict):
        return f"agent-chat returned unexpected JSON ({type(parsed).__name__}): {raw[:200]}"
    return (
        f"live version: {parsed.get('version', 'unknown')}"
        f" | status: {parsed.get('status', 'unknown')}"
        f" | ts: {parsed.get('ts', 'unknown')}"
    )


def main() -> int:
    # Read bytes and decode leniently rather than sys.stdin.read(). A response
    # cut mid-transfer by --max-time can end inside a multi-byte UTF-8
    # character, and strict decoding raises UnicodeDecodeError *before*
    # describe() ever sees the text -- which would traceback in exactly the
    # truncation case this script exists to report on.
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    print(describe(raw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
