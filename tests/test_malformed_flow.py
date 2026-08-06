"""Regression: the two crashes that motivated the harness now end gracefully.

Before payload validation, a reservation missing `dates` raised KeyError inside
build_quote and a null return datetime raised TypeError in return_utc_offset — both
surfacing to the customer as a dead-end apology with no escalation payload.
Run: python tests/test_malformed_flow.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import avis_client  # noqa: E402
import extend_flow  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from fixtures import DELETE, FakeResponse, fake_transport, reservation  # noqa: E402
from session import ServicingSession  # noqa: E402

avis_client.BACKOFF_BASE_S = 0


def test_malformed_payload_becomes_escalation_not_crash():
    """The full path: API sends junk -> boundary rejects -> classify_failure escalates
    with a handoff payload. No exception escapes to the CLI's generic catch."""
    session = ServicingSession()
    broken = reservation(dates=DELETE)
    with fake_transport([FakeResponse(200, broken)]):
        try:
            session.reservation = avis_client.get_reservation("AVS-48372915")
            raise AssertionError("boundary let a malformed payload through")
        except AvisAPIError as exc:
            outcome = extend_flow.classify_failure(session, exc, "lookup_reservation")
    assert outcome["escalate"] is True, outcome
    assert outcome["code"] == "MALFORMED_RESERVATION"
    assert "handoff" in outcome and session.escalated


def test_null_return_datetime_offset_is_safe():
    """Defense in depth: even if a null slips past, the offset accessor degrades to UTC
    instead of raising TypeError on len(None)."""
    session = ServicingSession()
    session.reservation = {"dates": {"current_return_datetime": None}}
    assert session.return_utc_offset == "+00:00"


def test_well_formed_payload_passes_untouched():
    payload = reservation()
    assert avis_client.validate_reservation(payload) is payload


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
