"""Scripted multi-turn driver for the dev/ sandbox, for QA conversation passes.

Feeds a fixed list of user turns through the real agent/session/gates, offline
(FakeBackend), and captures the full transcript including tool calls & results.

    python dev/scenario_runner.py <scenario> "turn 1" "turn 2" ...

Or import run_script() from a python snippet for more control. Not imported by
src/, not collected by pytest — dev-only tool, per dev/README.md conventions.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

DEV = Path(__file__).resolve().parent
ROOT = DEV.parent
SAMPLES = DEV / "sample_reservations"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(DEV))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import agent  # noqa: E402
from agents import SQLiteSession  # noqa: E402
from fake_backend import FakeBackend, patch_all, unpatch  # noqa: E402
from session import ServicingSession  # noqa: E402


def run_script(scenario: str, turns: list[str], label: str = "") -> list[dict]:
    """Run a scripted conversation against a local scenario. Returns transcript."""
    path = SAMPLES / f"{scenario}.json"
    payload = json.loads(path.read_text())
    backend = FakeBackend(payload)
    originals = patch_all(backend)

    transcript: list[dict] = []
    try:
        servicing_session = ServicingSession()
        history = SQLiteSession(f"avis-qa-{uuid.uuid4()}")

        for turn in turns:
            entry = {"user": turn}
            try:
                result = agent.run_turn(turn, servicing_session, history)
                entry["assistant"] = result.final_output
                # Try to capture tool calls from the run result, best-effort.
                tool_calls = []
                for item in getattr(result, "new_items", []) or []:
                    cls = type(item).__name__
                    if "ToolCall" in cls or "Tool" in cls:
                        tool_calls.append({
                            "type": cls,
                            "repr": str(item)[:2000],
                        })
                entry["tool_items"] = tool_calls
            except Exception as exc:  # noqa: BLE001
                entry["error"] = f"{type(exc).__name__}: {exc}"
            transcript.append(entry)
    finally:
        unpatch(originals)

    return transcript


def print_transcript(scenario: str, transcript: list[dict]) -> None:
    print(f"\n===== scenario: {scenario} =====")
    for i, entry in enumerate(transcript, 1):
        print(f"\n--- turn {i} ---")
        print(f"User: {entry['user']}")
        if "error" in entry:
            print(f"[ERROR] {entry['error']}")
            continue
        print(f"Agent: {entry['assistant']}")
        if entry.get("tool_items"):
            for t in entry["tool_items"]:
                print(f"  [tool_item] {t['type']}: {t['repr'][:300]}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python dev/scenario_runner.py <scenario> \"turn1\" \"turn2\" ...")
        raise SystemExit(1)
    scenario_name = sys.argv[1]
    user_turns = sys.argv[2:]
    tx = run_script(scenario_name, user_turns)
    print_transcript(scenario_name, tx)
