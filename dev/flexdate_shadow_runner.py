"""Shadow-mode driver for FLEXIBLE_DATE_ALTERNATIVES_MODE against the REAL Avis API.

Not imported by src/ or collected by pytest — same convention as the rest of dev/.
Deliberately does NOT use dev/fake_backend.py: this needs real /quote calls, driven by
the real agent, against the real test reservations from BRIEF.md.

Usage:
    FLEXIBLE_DATE_ALTERNATIVES_MODE=shadow python dev/flexdate_shadow_runner.py

Prints a transcript per conversation and, at the end, greps logs/agent.jsonl for the
date_alternatives_computed events emitted during this run and summarizes them.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

DEV = Path(__file__).resolve().parent
ROOT = DEV.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import agent  # noqa: E402
from agents import SQLiteSession  # noqa: E402
from session import ServicingSession  # noqa: E402

RESERVATIONS = [
    ("AVS-29471835", "sarah.johnson@example.com"),
    ("AVS-48372915", "marcus.lee@example.com"),
    ("AVS-77001020", "priya.patel@example.com"),
]

# A spread of target-date phrasings, cycled across reservations/conversations.
REQUESTS = [
    "extend to Thursday",
    "extend by 3 days",
    "extend one week",
    "extend to next Monday",
    "can I push the return back 2 days",
    "extend to Friday please",
    "I need it for 5 more days",
]


def extract_tool_calls(result) -> list[str]:
    lines = []
    try:
        items = result.new_items
    except AttributeError:
        return lines
    for item in items:
        cls = type(item).__name__
        if cls == "ToolCallItem":
            raw = getattr(item, "raw_item", None)
            lines.append(f"  TOOL CALL: {getattr(raw, 'name', None)}({getattr(raw, 'arguments', None)})")
        elif cls == "ToolCallOutputItem":
            lines.append(f"  TOOL RESULT: {str(getattr(item, 'output', None))[:600]}")
    return lines


def run_conversation(title: str, reservation_id: str, email: str, request: str, log) -> None:
    log(f"\n{'=' * 90}\n## {title}\n{'=' * 90}")
    servicing_session = ServicingSession()
    history = SQLiteSession(f"avis-flexdate-{uuid.uuid4()}")
    turns = [
        f"Hi, I'd like to extend reservation {reservation_id}",
        f"My email is {email}",
        request,
    ]
    for user_input in turns:
        log(f"\nYou: {user_input}")
        try:
            result = agent.run_turn(user_input, servicing_session, history)
            for line in extract_tool_calls(result):
                log(line)
            log(f"Agent: {result.final_output}")
        except Exception as exc:  # noqa: BLE001
            log(f"[EXCEPTION] {type(exc).__name__}: {exc}")
        time.sleep(0.2)


def main() -> int:
    mode = os.environ.get("FLEXIBLE_DATE_ALTERNATIVES_MODE", "off")
    if mode != "shadow":
        print(f"WARNING: FLEXIBLE_DATE_ALTERNATIVES_MODE={mode!r}, expected 'shadow'. "
              "Set it before running for real evidence.", file=sys.stderr)
    if not os.environ.get("AVIS_API_KEY"):
        print("ERROR: AVIS_API_KEY not set (check .env).", file=sys.stderr)
        return 1

    out_path = DEV / "flexdate_shadow_transcripts.txt"
    lines: list[str] = []

    def log(s: str) -> None:
        print(s)
        lines.append(s)

    log(f"[run started] mode={mode} time={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")

    run_marker_start = time.time()

    n = 0
    for (rid, email) in RESERVATIONS:
        for i, req in enumerate(REQUESTS):
            n += 1
            run_conversation(f"{n}. {rid} / {req!r}", rid, email, req, log)
            if n >= 21:  # ~ 3 reservations x 7 requests
                break
        if n >= 21:
            break

    out_path.write_text("\n".join(lines))
    log(f"\n[run complete] {n} conversations. Transcript written to {out_path}")
    log(f"[run window] start_epoch={run_marker_start} end_epoch={time.time()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
