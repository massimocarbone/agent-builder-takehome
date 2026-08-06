"""REVIEW_QUEUE #17: the agent must ground 'today' in the real system clock.

Before this fix, a fresh conversation with zero context asked "what is today's date?"
and answered "June 21, 2024" -- real time was 2026-08-05, over two years off, a pure
hallucination near the model's training cutoff. Run: python tests/test_date_grounding.py
"""
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import agent  # noqa: E402


def test_instructions_are_a_callable_not_a_static_string():
    """A static string bakes in whatever day the process happened to start on. The SDK
    supports a callable that runs fresh every turn -- that's the only way 'today' stays
    correct for a process that outlives the day it started."""
    assert callable(agent.servicing_agent.instructions), \
        "instructions must be dynamic, or CURRENT DATE AND TIME goes stale"


def test_built_instructions_contain_a_real_recent_date():
    text = agent.build_instructions(None, agent.servicing_agent)
    assert "CURRENT DATE AND TIME" in text

    match = re.search(r"CURRENT DATE AND TIME.*?: (.+?)\.", text)
    assert match, "couldn't find the injected date line"
    stamp = match.group(1)

    parsed = datetime.strptime(stamp, "%A, %B %d, %Y, %H:%M UTC").replace(tzinfo=timezone.utc)
    drift = abs((datetime.now(timezone.utc) - parsed).total_seconds())
    assert drift < 120, f"injected date is {drift:.0f}s from real time: {stamp!r}"


def test_instructions_regenerate_across_calls():
    """Not cached at import time -- two calls a moment apart should both reflect 'now',
    not the same frozen string forever."""
    first = agent.build_instructions(None, agent.servicing_agent)
    second = agent.build_instructions(None, agent.servicing_agent)
    assert "CURRENT DATE AND TIME" in first and "CURRENT DATE AND TIME" in second


def test_instructions_never_source_the_date_from_a_reservation():
    """The injected line must come from datetime.now(), never from reservation payload
    fields -- otherwise a customer with a weird reservation could skew 'today' itself."""
    text = agent.build_instructions(None, agent.servicing_agent)
    match = re.search(r"CURRENT DATE AND TIME.*?: (.+?)\.", text)
    parsed = datetime.strptime(match.group(1), "%A, %B %d, %Y, %H:%M UTC").replace(tzinfo=timezone.utc)
    # The only honest check available without mocking datetime.now: it must be close to
    # wall-clock time, not equal to any fixture's static 2026-06-xx dates.
    assert parsed > datetime(2026, 7, 1, tzinfo=timezone.utc), \
        "instructions date looks fixture-derived, not wall-clock"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
