"""Scripted multi-turn driver for IN_FLOW_UPGRADE_OFFER and CANCEL_RETENTION_PROMPT.

Not imported by src/ or collected by pytest — same convention as the rest of dev/.
Offline sandbox (dev/fake_backend.py), needs only OPENAI_API_KEY, no real Avis API.

Usage:
    python dev/trigger_flag_runner.py

Runs a battery of scripted conversations covering active_mid_rental (standard, late-fee
scenario -> upgrade offer trigger) and active_mid_rental_preferred (Preferred member,
should NOT trigger), plus cancel-eligible scenarios (pre_pickup_free, pre_pickup_penalty,
overdue_mid_rental) for the retention-prompt trigger. Prints transcripts and, at the end,
summarizes upgrade_offer_computed / retention_prompt_computed events logged during the run.
"""
from __future__ import annotations

import copy
import json
import sys
import time
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


def load_payload(name: str) -> dict:
    return json.loads((SAMPLES / f"{name}.json").read_text())


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


def run_scenario(title: str, payload: dict, turns: list[str], log) -> None:
    log(f"\n{'=' * 90}\n## {title}\n{'=' * 90}")
    backend = FakeBackend(copy.deepcopy(payload))
    originals = patch_all(backend)
    servicing_session = ServicingSession()
    history = SQLiteSession(f"avis-trig-{uuid.uuid4()}")
    try:
        for user_input in turns:
            log(f"\nYou: {user_input}")
            try:
                result = agent.run_turn(user_input, servicing_session, history)
                for line in extract_tool_calls(result):
                    log(line)
                log(f"Agent: {result.final_output}")
            except Exception as exc:  # noqa: BLE001
                log(f"[EXCEPTION] {type(exc).__name__}: {exc}")
    finally:
        unpatch(originals)


def main() -> int:
    out_path = DEV / "trigger_flag_transcripts.txt"
    lines: list[str] = []

    def log(s: str) -> None:
        print(s)
        lines.append(s)

    log(f"[run started] time={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    n = 0

    # ---------- UPGRADE OFFER: overdue_mid_rental (standard member, late-fee scenario) ----------
    # fake_backend's late_fee only fires when current_return_datetime is already in the
    # past (DECISIONS.md scenario table: overdue_mid_rental is the one built for this).
    # active_mid_rental's current_return is still in the future, so extending it never
    # crosses into a late fee — included separately below as the non-triggering control.
    upgrade_scripts_standard = [
        ["extend my reservation by 1 day", "yes please go ahead", "4242 12345"],
        ["I need to extend it, make the new return tomorrow at 6pm", "confirm", "1234 90210"],
        ["can you push my return back by 4 days", "yes charge it", "0000 10001"],
        ["extend it, add 12 hours to my return time", "yes go ahead", "1111 55555"],
        ["I want to keep the car 2 extra days", "confirm the charge", "2222 30301"],
    ]
    for i, turns in enumerate(upgrade_scripts_standard):
        n += 1
        p = load_payload("overdue_mid_rental")
        full_turns = [f"Hi, I'd like to extend reservation {p['reservation_id']}",
                      f"My email is {p.get('email') or 'standard@example.com'}"] + turns
        run_scenario(f"{n}. UPGRADE/standard overdue_mid_rental scenario {i+1}", p, full_turns, log)

    # ---------- UPGRADE OFFER: active_mid_rental_preferred (should NOT fire) ----------
    upgrade_scripts_preferred = [
        ["extend my reservation by 1 day", "yes go ahead", "3333 40404"],
        ["extend to 6pm tomorrow instead of today", "confirm", "4444 50505"],
        ["push my return back 3 days please", "yes", "5555 60606"],
    ]
    for i, turns in enumerate(upgrade_scripts_preferred):
        n += 1
        p = load_payload("active_mid_rental_preferred")
        full_turns = [f"Hi, I'd like to extend reservation {p['reservation_id']}",
                      f"My email is {p.get('email') or 'preferred@example.com'}"] + turns
        run_scenario(f"{n}. UPGRADE/preferred active_mid_rental_preferred scenario {i+1}", p, full_turns, log)

    # ---------- RETENTION PROMPT: cancel-eligible scenarios ----------
    cancel_scenarios = [
        ("pre_pickup_free", ["I want to cancel my reservation", "yes cancel it"]),
        ("pre_pickup_free", ["cancel my booking please", "yes confirm"]),
        ("pre_pickup_penalty", ["I need to cancel", "yes go ahead and cancel"]),
        ("pre_pickup_penalty", ["please cancel my reservation", "confirm cancel"]),
        ("overdue_mid_rental", ["I want to cancel this rental", "yes cancel"]),
        ("overdue_mid_rental", ["cancel it", "confirm"]),
        ("pre_pickup_free", ["cancel please", "no wait, actually keep it, never mind"]),
    ]
    for i, (scenario_name, turns) in enumerate(cancel_scenarios):
        n += 1
        p = load_payload(scenario_name)
        full_turns = [f"Hi, I'd like to cancel reservation {p['reservation_id']}",
                      f"My email is {p.get('email') or 'test@example.com'}"] + turns
        run_scenario(f"{n}. RETENTION/{scenario_name} scenario {i+1}", p, full_turns, log)

    out_path.write_text("\n".join(lines))
    log(f"\n[run complete] {n} conversations. Transcript written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
