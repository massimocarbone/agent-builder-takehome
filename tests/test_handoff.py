"""Regression tests for the 2026-08-05 handoff failures (REVIEW_QUEUE #12, #13).

The agent announced a handoff and then serviced the request itself 19 seconds later.
These prove the terminal state is now enforced in code, not requested in a prompt.
Run: python tests/test_handoff.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
from session import ServicingSession, redact_text  # noqa: E402


def _call(tool, session, **kwargs):
    """Invoke a @function_tool's underlying implementation with a fake run context."""
    class Ctx:
        context = session
    return tool.__wrapped__(Ctx(), **kwargs) if hasattr(tool, "__wrapped__") else None


def test_hard_handoff_blocks_every_action_tool():
    session = ServicingSession()
    session.handed_off = True
    for name, kwargs in [("lookup_reservation", {"reservation_id": "AVS-48372915"}),
                         ("quote_extension", {"new_return_datetime": "2026-06-12"}),
                         ("confirm_extension", {"email": "marcus.lee@example.com",
                                                "cvv": "123", "billing_zip": "60601"})]:
        out = agent._blocked(session)
        assert out is not None and out["handed_off"] is True, name


def test_open_session_is_not_blocked():
    assert agent._blocked(ServicingSession()) is None


def test_assistive_escalation_stays_open():
    """A customer who can't find their reservation id often finds it a turn later.
    Hard-terminating there would be its own bad experience."""
    session = ServicingSession()
    session.escalated = True
    session.handed_off = False          # what kind="assistive" sets
    assert agent._blocked(session) is None, "assistive escalation must not go terminal"


def test_transcript_reaches_the_handoff_payload():
    session = ServicingSession()
    session.record("customer", "i need to keep the car longer")
    session.record("agent", "sure — until when?")
    payload = session.escalation_payload()
    assert payload["transcript"] == [
        {"role": "customer", "text": "i need to keep the car longer"},
        {"role": "agent", "text": "sure — until when?"},
    ], payload["transcript"]


def test_card_secrets_scrubbed_from_transcript():
    """Customers type card codes straight into chat; the transcript is handed to a human
    and written to logs."""
    session = ServicingSession()
    session.record("customer", "email is a@b.com, cvv is 847 and zip 90210")
    session.record("customer", "card is 4111 1111 1111 1111")
    joined = " ".join(t["text"] for t in session.transcript)
    assert "847" not in joined, joined
    assert "4111" not in joined, joined
    assert "a@b.com" in joined, "over-redacted: email is needed for the handoff"


def test_redaction_variants():
    assert "123" not in redact_text("CVV: 123")
    assert "4321" not in redact_text("the cvc is 4321")
    assert "999" not in redact_text("security code 999")
    assert redact_text("i want to return june 12") == "i want to return june 12"


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
