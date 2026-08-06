"""Cancel workflow tests: policy branches, gates, and the variance check.

The pre-pickup branches are unreachable through any live test reservation (all six are
months past their return date) — this file is the only place that code has ever run,
which is the reason the fixture harness exists. Run: python tests/test_cancel_flow.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cancel_flow  # noqa: E402
import config  # noqa: E402
import policy  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from extend_flow import FlowError  # noqa: E402
from fixtures import reservation, reservation_pay_at_counter, reservation_pre_pickup  # noqa: E402
from session import ServicingSession  # noqa: E402


def _stub_cancel(details=None, error=None):
    orig = cancel_flow.cancel_reservation

    def fake(reservation_id, email, reason=None):
        if error:
            raise error
        return {"success": True, "confirmation_number": "CXL-TEST-1",
                "cancellation_details": details}
    cancel_flow.cancel_reservation = fake
    return lambda: setattr(cancel_flow, "cancel_reservation", orig)


def _staged(res):
    """Session with an estimate the customer has 'seen' (turn boundary satisfied).

    Mid-rental reservations also need the early-return-vs-cancel question answered, so
    these tests answer it the way the customer would have to: explicitly, on the turn
    after they were asked. Tests that mean to probe the gate itself do NOT use this.
    """
    session = ServicingSession()
    session.load_reservation(res)
    session.turn = 1
    cancel_flow.build_cancel_estimate(session)
    session.turn = 2
    if session.pending_cancellation.requires_disambiguation:
        cancel_flow.resolve_cancel_intent(session, cancel_flow.TRUE_CANCELLATION)
    return session


# --- policy branches (pure computation, no stubs needed) ----------------------------

def test_pre_pickup_over_48h_full_refund():
    """The branch no live reservation can reach: >48h out, no penalty, full refund."""
    est = policy.compute_cancel_estimate(reservation_pre_pickup(hours_until_pickup=72))
    assert est.branch == policy.PRE_PICKUP_FREE
    assert est.penalty == 0.0
    assert est.refund == est.prepaid > 0
    assert not est.requires_disambiguation


def test_pre_pickup_within_48h_one_day_penalty():
    res = reservation_pre_pickup(hours_until_pickup=6)
    est = policy.compute_cancel_estimate(res)
    assert est.branch == policy.PRE_PICKUP_PENALTY
    assert est.penalty == round(res["pricing"]["daily_rate"], 2)
    assert est.refund == round(est.prepaid - est.penalty, 2)
    assert not est.requires_disambiguation


def test_exactly_at_boundary_uses_penalty_branch():
    """48h exactly is 'within 48 hours' per kb_can_01's 'more than 48 hours' wording."""
    est = policy.compute_cancel_estimate(reservation_pre_pickup(hours_until_pickup=48))
    assert est.branch == policy.PRE_PICKUP_PENALTY


def test_in_rental_requires_disambiguation():
    """Pickup in the past = customer holds the car. 'Cancel' may mean early return."""
    est = policy.compute_cancel_estimate(reservation())  # live capture: months overdue
    assert est.branch == policy.IN_RENTAL
    assert est.requires_disambiguation is True
    assert est.penalty == round(reservation()["pricing"]["daily_rate"], 2)


def test_in_rental_estimate_matches_live_api_formula():
    """Observed on all six live reservations: penalty == daily_rate,
    refund == prepaid - penalty. The estimator must reproduce that exactly."""
    res = reservation()
    est = policy.compute_cancel_estimate(res)
    assert est.penalty == res["pricing"]["daily_rate"]
    assert est.refund == round(res["payment"]["total_charged"] - est.penalty, 2)


def test_pay_at_counter_nothing_to_refund():
    est = policy.compute_cancel_estimate(reservation_pay_at_counter())
    assert est.penalty == 0.0 and est.refund == 0.0 and est.prepaid == 0.0
    assert "nothing to refund" in est.caveats[0]


def test_penalty_never_exceeds_prepaid():
    """A $50/day rate against a $30 prepaid must not 'refund' a negative amount."""
    res = reservation_pre_pickup(hours_until_pickup=6)
    res["payment"]["total_charged"] = 30.0
    res["pricing"]["daily_rate"] = 50.0
    est = policy.compute_cancel_estimate(res)
    assert est.penalty == 30.0 and est.refund == 0.0


def test_every_estimate_carries_citation_and_caveats():
    for res in [reservation_pre_pickup(72), reservation_pre_pickup(6), reservation()]:
        est = policy.compute_cancel_estimate(res)
        assert est.citation["article_id"] == "kb_can_01"
        assert est.caveats, "estimate must carry the non-refundable-rate caveat"


# --- flow gates ---------------------------------------------------------------------

def test_no_cancel_without_estimate():
    restore = _stub_cancel()
    try:
        session = ServicingSession()
        session.load_reservation(reservation())
        out = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert out["ok"] is False and "estimate" in out["customer_message"].lower(), out
    finally:
        restore()


def test_no_cancel_on_the_same_turn_as_the_estimate():
    restore = _stub_cancel()
    try:
        session = ServicingSession()
        session.load_reservation(reservation())
        session.turn = 1
        cancel_flow.build_cancel_estimate(session)
        out = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert out["ok"] is False and "not seen" in out["customer_message"], out
    finally:
        restore()


def test_cancel_cannot_run_twice():
    details = {"penalty": 38.99, "refund_amount": 77.98, "prepaid_amount": 116.97,
               "currency": "USD"}
    restore = _stub_cancel(details=details)
    try:
        session = _staged(reservation())
        first = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert first["ok"] is True
        second = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert second["ok"] is False and second.get("already_processed") is True, second
    finally:
        restore()


def test_inactive_reservation_refused_at_estimate():
    session = ServicingSession()
    session.load_reservation(reservation(status="cancelled"))
    try:
        cancel_flow.build_cancel_estimate(session)
        raise AssertionError("estimated a cancellation for a non-active reservation")
    except FlowError as exc:
        assert "not active" in str(exc)


def test_verification_lockout_hands_off():
    restore = _stub_cancel(error=AvisAPIError("VERIFICATION_FAILED", "no", status=403))
    try:
        session = _staged(reservation())
        for _ in range(config.MAX_VERIFICATION_ATTEMPTS):
            out = cancel_flow.commit_cancellation(session, "wrong@example.com")
        assert out.get("escalate") is True and session.handed_off is True, out
    finally:
        restore()


# --- variance check (the load-bearing safety net) -----------------------------------

def test_matching_outcome_completes_quietly():
    details = {"penalty": 38.99, "refund_amount": 77.98, "prepaid_amount": 116.97,
               "currency": "USD"}
    restore = _stub_cancel(details=details)
    try:
        out = cancel_flow.commit_cancellation(_staged(reservation()), "marcus.lee@example.com")
        assert out["ok"] is True and "escalate" not in out, out
        assert out["confirmation_number"] == "CXL-TEST-1"
    finally:
        restore()


def test_adverse_outcome_escalates_and_terminates():
    """Penalty came back higher than the customer agreed to → remediation handoff."""
    details = {"penalty": 120.00, "refund_amount": 0.0, "prepaid_amount": 116.97,
               "currency": "USD"}
    restore = _stub_cancel(details=details)
    try:
        session = _staged(reservation())
        out = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert out["ok"] is True, "the cancellation DID happen; honesty requires saying so"
        assert out.get("escalate") is True, out
        assert session.handed_off is True, "adverse variance must terminate the session"
        assert out["variance"]["actual_penalty"] == 120.00
        assert out["handoff"]["pending_cancellation"]["consumed"] is True
    finally:
        restore()


def test_favorable_outcome_reports_good_news_without_escalating():
    details = {"penalty": 0.0, "refund_amount": 116.97, "prepaid_amount": 116.97,
               "currency": "USD"}
    restore = _stub_cancel(details=details)
    try:
        session = _staged(reservation())
        out = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert out["ok"] is True and "escalate" not in out, out
        assert out.get("better_than_estimate") is True
        assert session.handed_off is False, "good news must not burn a human handoff"
    finally:
        restore()


def test_sub_threshold_variance_does_not_escalate():
    details = {"penalty": 39.50, "refund_amount": 77.47, "prepaid_amount": 116.97,
               "currency": "USD"}  # 51 cents adverse, threshold is $1
    restore = _stub_cancel(details=details)
    try:
        out = cancel_flow.commit_cancellation(_staged(reservation()), "marcus.lee@example.com")
        assert out["ok"] is True and "escalate" not in out, out
    finally:
        restore()


# --- disambiguation & retention -----------------------------------------------------

def test_in_rental_cancel_is_blocked_until_intent_is_resolved():
    """The disambiguation was advisory until the write started reading it."""
    writes = []
    restore = _stub_cancel(details={"penalty": 38.99, "refund_amount": 77.98,
                                    "prepaid_amount": 116.97, "currency": "USD"})
    try:
        session = ServicingSession()
        session.load_reservation(reservation())
        session.turn = 1
        cancel_flow.build_cancel_estimate(session)
        session.turn = 2
        out = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert out["ok"] is False and out.get("requires_disambiguation") is True, out
        assert session.pending_cancellation.consumed is False, out
    finally:
        restore()


def test_early_return_intent_discards_the_staged_cancellation():
    """'I'll bring it back' must leave nothing a later tool call could commit."""
    session = ServicingSession()
    session.load_reservation(reservation())
    session.turn = 1
    cancel_flow.build_cancel_estimate(session)
    session.turn = 2
    out = cancel_flow.resolve_cancel_intent(session, cancel_flow.EARLY_RETURN)
    assert out["ok"] is True and out["nothing_cancelled"] is True, out
    assert session.pending_cancellation is None, "an early return left a committable estimate"

    restore = _stub_cancel(details={"penalty": 38.99, "refund_amount": 77.98,
                                    "prepaid_amount": 116.97, "currency": "USD"})
    try:
        session.turn = 3
        follow_up = cancel_flow.commit_cancellation(session, "marcus.lee@example.com")
        assert follow_up["ok"] is False, follow_up
    finally:
        restore()


def test_ambiguous_intent_is_not_accepted_as_an_answer():
    """The model must not launder an unclear reply into a resolved intent."""
    session = ServicingSession()
    session.load_reservation(reservation())
    session.turn = 1
    cancel_flow.build_cancel_estimate(session)
    session.turn = 2
    for guess in ["maybe", "cancel", "", "yes"]:
        out = cancel_flow.resolve_cancel_intent(session, guess)
        assert out["ok"] is False, f"{guess!r} was accepted as a resolved intent: {out}"
    assert session.pending_cancellation.intent_confirmed is False


def test_in_rental_summary_carries_early_return_alternative():
    session = ServicingSession()
    session.load_reservation(reservation())
    session.turn = 1
    summary = cancel_flow.build_cancel_estimate(session)
    assert summary["requires_disambiguation"] is True
    alt = summary["early_return_alternative"]
    assert alt["citation"]["article_id"] == "kb_can_04"
    assert "no fee" in alt["what"].lower() or "no fee" in alt["warning"].lower() or \
           "No fee" in alt["what"], alt


def test_pre_pickup_summary_needs_no_disambiguation():
    session = ServicingSession()
    session.load_reservation(reservation_pre_pickup(72))
    session.turn = 1
    summary = cancel_flow.build_cancel_estimate(session)
    assert "requires_disambiguation" not in summary


def test_retention_prompt_is_shadow_logged_when_off():
    assert config.CANCEL_RETENTION_PROMPT is False, "flag should default off"
    session = ServicingSession()
    session.load_reservation(reservation_pre_pickup(72))
    session.turn = 1
    cancel_flow.build_cancel_estimate(session)
    assert not session.collected_context.get("retention_prompt_offered"), \
        "flag off must not mark the offer as made"


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
