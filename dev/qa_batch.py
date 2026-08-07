"""Batch-runs several scripted conversations across all 7 sandbox scenarios and
dumps full transcripts (with tool call summaries) to a text file for manual QA
review. Dev-only, not imported by src/, not collected by pytest."""
from __future__ import annotations

import sys
from pathlib import Path

DEV = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV))

from scenario_runner import run_script  # noqa: E402

CONVERSATIONS = [
    ("active_mid_rental", "happy_path_extend", [
        "hey, can I extend my rental a few days?",
        "AVS-90000001 / sarah.johnson@example.com... wait is that even the right email? let me check. yes sarah.johnson@example.com",
        "can I push the return to Aug 12th around 3pm?",
        "actually wait, what's the grace period if I'm a little late returning it?",
        "ok that's fine, yes go ahead and extend it",
    ]),
    ("active_mid_rental", "read_only_question", [
        "what car am I driving and when is it due back? reservation AVS-90000001",
        "sarah.johnson@example.com",
        "cool thanks, no changes needed right now",
    ]),
    ("active_mid_rental_preferred", "preferred_upgrade_mention", [
        "hi I need to extend AVS-90000002, email marcus.lee@example.com",
        "til Aug 13 5pm please",
        "yes confirm it",
    ]),
    ("pre_pickup_free", "happy_path_cancel_free", [
        "I need to cancel my upcoming reservation AVS-90000003",
        "priya.patel@example.com",
        "yes please cancel it",
    ]),
    ("pre_pickup_penalty", "happy_path_cancel_penalty", [
        "I need to cancel reservation AVS-90000004, my pickup is tomorrow morning basically",
        "david.kim@example.com",
        "will I get charged anything?",
        "ok, yes cancel it, I understand",
    ]),
    ("overdue_mid_rental", "cancel_ambiguous_disambiguation", [
        "I want to cancel AVS-90000005",
        "tomas.rivera@example.com",
        "I want to cancel",
    ]),
    ("overdue_mid_rental", "cancel_ambiguous_early_return", [
        "hey I have reservation AVS-90000005 and I want to cancel it",
        "tomas.rivera@example.com",
        "I'm done with the car, just bringing it back today",
    ]),
    ("pay_at_counter", "nothing_to_refund", [
        "can you cancel my reservation AVS-90000006? email robert.chen@example.com",
        "yes cancel it",
        "wait will I get a refund?",
    ]),
    ("inactive_cancelled", "already_cancelled_409", [
        "I need to cancel AVS-90000007, email is whatever is on file, let's say a placeholder@example.com",
        "that doesn't sound right, I never cancelled this, can you check again?",
    ]),
]


def main() -> None:
    out_lines = []
    for scenario, label, turns in CONVERSATIONS:
        tx = run_script(scenario, turns, label=label)
        out_lines.append(f"\n\n########## SCENARIO: {scenario} | CONVO: {label} ##########")
        for i, entry in enumerate(tx, 1):
            out_lines.append(f"\n--- turn {i} ---")
            out_lines.append(f"USER: {entry['user']}")
            if "error" in entry:
                out_lines.append(f"[RUNTIME ERROR] {entry['error']}")
                continue
            out_lines.append(f"AGENT: {entry['assistant']}")
            for t in entry.get("tool_items", []):
                out_lines.append(f"  [tool_item] {t['type']}: {t['repr'][:500]}")
        print(f"done: {scenario} / {label}")

    out_path = DEV / "qa_transcripts_raw.txt"
    out_path.write_text("\n".join(out_lines))
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
