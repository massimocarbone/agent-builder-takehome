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
    """Invoke a @function_tool's real implementation with a fake run context.

    function_tool wraps the function but keeps it on __wrapped__. Calling that is what
    makes this a real test — asserting on _blocked() directly would pass even if no tool
    ever consulted it.
    """
    class Ctx:
        context = session
    return tool.__wrapped__(Ctx(), **kwargs)


def test_hard_handoff_blocks_every_action_tool():
    """Each tool must consult the terminal state itself. If one forgets, the agent can
    still act after announcing a handoff — the exact 2026-08-05 failure."""
    cases = [
        (agent.lookup_reservation, {"reservation_id": "AVS-48372915"}),
        (agent.quote_extension, {"new_return_datetime": "2026-06-12"}),
        (agent.confirm_extension, {"email": "marcus.lee@example.com",
                                   "cvv": "123", "billing_zip": "60601"}),
    ]
    for tool, kwargs in cases:
        session = ServicingSession()
        session.handed_off = True
        out = _call(tool, session, **kwargs)
        assert out.get("handed_off") is True, f"{tool.name} acted after handoff: {out}"
        assert out["ok"] is False, tool.name


def test_blocked_tools_make_no_api_call():
    """Refusal must be structural, not a post-hoc filter — a blocked lookup that still
    hits the API would leak reservation data into a handed-off session."""
    import avis_client
    calls = []
    original = avis_client.get_reservation
    avis_client.get_reservation = lambda rid: calls.append(rid)
    try:
        session = ServicingSession()
        session.handed_off = True
        _call(agent.lookup_reservation, session, reservation_id="AVS-48372915")
    finally:
        avis_client.get_reservation = original
    assert calls == [], f"blocked tool still called the API: {calls}"


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


def test_card_last_four_withheld_until_verified():
    """Reads are open to anyone holding a reservation id, and supplying an email is a
    claim rather than proof. Enforced by omission: the model never receives the digits."""
    import avis_client
    from fixtures import reservation as fixture
    orig = avis_client.get_reservation
    avis_client.get_reservation = lambda rid: fixture()
    try:
        unverified = ServicingSession()
        out = _call(agent.lookup_reservation, unverified, reservation_id="AVS-48372915")
        assert out["card_last_four"] == "withheld until verified", out["card_last_four"]

        verified = ServicingSession()
        verified.verified_email = "marcus.lee@example.com"
        out = _call(agent.lookup_reservation, verified, reservation_id="AVS-48372915")
        assert out["card_last_four"].isdigit(), out["card_last_four"]
    finally:
        avis_client.get_reservation = orig


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
