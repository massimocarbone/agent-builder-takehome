"""Mutation testing on core safety invariants — verify no test passes silently (DECISIONS.md §2 & §3).

Not imported by src/ or collected by pytest — standalone dev script for the comprehensive
testing pass. For each safety guard, temporarily removes it, runs the suite, and reports
whether tests catch the absence.

Invariants under test:
1. Confirmation gate: quote must exist, be staged on an earlier turn, and not already consumed
2. Cancel intent: early_return vs true_cancellation must be resolved before write
3. Legacy suppression: legacy articles suppressed when higher-authority articles in same category exist
4. Post-write reconciliation: actual charges compared to quoted; material variance escalates
5. Idempotency: same write doesn't execute twice on the same quote

Each mutation is applied, the full test suite runs, we record which tests fail, then revert.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

MUTATIONS = {
    "extend_consumed_flag": {
        "file": SRC / "extend_flow.py",
        "original": "    if session.pending_quote.consumed:",
        "mutant": "    if False:  # MUTATION: disabled consumed check",
        "description": "Removes the single-use gate on extension writes — same quote can be charged twice.",
    },
    "extend_turn_boundary": {
        "file": SRC / "extend_flow.py",
        "original": "    if session.turn <= session.pending_quote.quoted_on_turn:",
        "mutant": "    if False:  # MUTATION: disabled turn boundary check",
        "description": "Removes the turn-boundary gate on extension writes — quotes can be "
                      "charged on the same turn they were staged.",
    },
    "cancel_consumed_flag": {
        "file": SRC / "cancel_flow.py",
        "original": "    if staged.consumed:",
        "mutant": "    if False:  # MUTATION: disabled consumed check",
        "description": "Removes the single-use gate on cancellation writes — same estimate can be used twice.",
    },
    "cancel_turn_boundary": {
        "file": SRC / "cancel_flow.py",
        "original": "    if session.turn <= staged.quoted_on_turn:",
        "mutant": "    if False:  # MUTATION: disabled turn boundary check",
        "description": "Removes the turn-boundary gate on cancellation writes — estimates can be "
                      "processed on the same turn they were staged.",
    },
    "cancel_intent_confirmation": {
        "file": SRC / "cancel_flow.py",
        "original": "    if staged.requires_disambiguation and not staged.intent_confirmed:",
        "mutant": "    if False:  # MUTATION: disabled intent confirmation guard",
        "description": "Removes the requirement to resolve early-return vs true-cancellation "
                      "before writing — ambiguous intent can proceed to cancel.",
    },
    "legacy_article_suppression": {
        "file": SRC / "kb.py",
        "original": "        if article[\"authority\"] != \"legacy\":",
        "mutant": "        if False:  # MUTATION: disabled legacy suppression",
        "description": "Removes the authority-based legacy article suppression — stale "
                      "articles can be returned alongside official ones.",
    },
}


def apply_mutation(mutation: dict) -> bool:
    """Apply one mutation, return True if successful."""
    filepath = mutation["file"]
    original = mutation["original"]
    mutant = mutation["mutant"]

    content = filepath.read_text()
    if original not in content:
        print(f"❌ Could not find original text in {filepath.name}:")
        print(f"   Looking for: {original[:60]}...")
        return False

    mutated = content.replace(original, mutant, 1)
    filepath.write_text(mutated)
    return True


def revert_mutation(mutation: dict) -> None:
    """Revert one mutation."""
    filepath = mutation["file"]
    original = mutation["original"]
    mutant = mutation["mutant"]

    content = filepath.read_text()
    reverted = content.replace(mutant, original, 1)
    filepath.write_text(reverted)


def run_tests() -> tuple[int, str]:
    """Run pytest, return (exit_code, stdout+stderr)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--tb=short", "-q"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode, result.stdout + result.stderr


def main() -> None:
    print("=" * 70)
    print("MUTATION TESTING: Safety Invariants")
    print("=" * 70)
    print()

    # Baseline: run tests unmodified
    print("Running baseline (unmodified code)...")
    baseline_code, baseline_output = run_tests()
    # Extract pass/fail counts from pytest output
    baseline_summary = baseline_output.split("\n")[-2] if baseline_output else "unknown"
    print(f"Baseline result: {baseline_summary}")
    print()

    results = []

    for name, mutation in MUTATIONS.items():
        print(f"Mutation: {name}")
        print(f"  {mutation['description']}")

        if not apply_mutation(mutation):
            print(f"  ❌ Failed to apply mutation")
            results.append((name, "FAILED_TO_APPLY", None, None))
            continue

        exit_code, output = run_tests()
        summary = output.split("\n")[-2] if output else "unknown"

        if exit_code == 0:
            # Tests still pass — this is BAD, means the mutation wasn't caught
            print(f"  ⚠️  Tests still PASS (mutation not caught!)")
            results.append((name, "NOT_CAUGHT", summary, None))
        else:
            # Tests fail — this is GOOD, means the mutation was caught
            print(f"  ✓ Tests FAIL (mutation caught)")
            results.append((name, "CAUGHT", summary, None))

        revert_mutation(mutation)
        print()

    # Summary
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    caught = sum(1 for _, status, _, _ in results if status == "CAUGHT")
    not_caught = sum(1 for _, status, _, _ in results if status == "NOT_CAUGHT")
    failed = sum(1 for _, status, _, _ in results if status == "FAILED_TO_APPLY")

    print(f"Mutations applied: {len(results)}")
    print(f"  ✓ Caught by tests: {caught}")
    print(f"  ⚠️  NOT caught (gap): {not_caught}")
    print(f"  ❌ Failed to apply: {failed}")
    print()

    if not_caught > 0:
        print("FINDINGS — mutations not caught by tests (potential coverage gaps):")
        for name, status, summary, _ in results:
            if status == "NOT_CAUGHT":
                print(f"  - {name}: {MUTATIONS[name]['description']}")
        print()

    if baseline_code != 0:
        print("⚠️  WARNING: Baseline tests did not pass. Results may be unreliable.")


if __name__ == "__main__":
    main()
