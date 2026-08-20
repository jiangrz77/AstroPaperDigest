"""Structured pipeline progress events, shared between the CLI and the web UI.

Any step of the pipeline can emit a machine-readable progress line:

    PROGRESS {"stage": "rank", "done": 1, "total": 4, "message": "AI ranking… batch 2/4"}

- stage: one of profile / fetch / filter / rank / output / done / error
- done:  number of completed units (0 when unknown)
- total: total units (0 means indeterminate progress)
- message: short human-readable detail for the current moment

The web UI parses these lines to drive a progress bar; humans running the
CLI see the same lines as readable status text.
"""

import json

PREFIX = "PROGRESS "


def emit(stage: str, done: int = 0, total: int = 0, message: str = "") -> None:
    """Print one machine-readable progress event to stdout."""
    payload = {
        "stage": str(stage),
        "done": int(done),
        "total": int(total),
        "message": str(message),
    }
    print(f"{PREFIX}{json.dumps(payload, ensure_ascii=False)}", flush=True)


def parse(line: str):
    """Parse a progress event from one output line, or None if not one."""
    if not line.startswith(PREFIX):
        return None
    try:
        data = json.loads(line[len(PREFIX):].strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
