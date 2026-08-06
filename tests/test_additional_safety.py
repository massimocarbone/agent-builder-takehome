"""Adversarial regression tests for safety claims not covered by the original suite.

These tests assert the customer/revenue protections described in BRIEF.md and
DECISIONS.md. A failure means the implementation does not structurally enforce that
protection; it is not a flaky live-LLM evaluation.

Most started life as xfails from an external testing pass and have since been fixed and
graduated. What remains marked `@deferred` is a gap we decided not to close, with the
reason attached — strict mode means fixing one fails the suite until the marker goes.

Run: python -m pytest tests/test_additional_safety.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import avis_client  # noqa: E402
import cancel_flow  # noqa: E402
import config  # noqa: E402
import extend_flow  # noqa: E402
import kb  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from fixtures import (  # noqa: E402
    EXTEND_TEST_NOW, FakeResponse, error_body, fake_transport, reservation,
)
from session import ServicingSession, redact_text  # noqa: E402


# Strict xfail marks a gap we have decided NOT to close, with the reason attached. It is
# applied per-test, never to the module: a module-level marker also swallows *errors*, so a
# test that crashes before reaching its assertion reports as a documented gap while proving
# nothing. That is exactly what happened on the first pass here — four tests died in
# `_stage_quote` on a stale fixture date and were read as findings (DECISIONS.md §2).
def deferred(reason: str):
    return pytest.mark.xfail(reason=reason, strict=True)


QUOTE = {"success": True, "quote": {"charges": {
    "daily_rate": 38.99,
    "extension_days": 3,
    "subtotal": 116.97,
    "late_fee": 29.0,
    "one_way_fee": 0.0,
    "taxes_and_fees": 13.5,
    "total_charged": 159.47,
    "currency": "USD",
}}}


def _session() -> ServicingSession:
    session = ServicingSession()
    session.load_reservation(reservation())
    return session


def _stage_quote(session: ServicingSession, quote: dict = QUOTE) -> None:
    original = extend_flow.quote_change
    original_mode = config.FLEXIBLE_DATE_ALTERNATIVES_MODE
    extend_flow.quote_change = lambda *args, **kwargs: quote
    config.FLEXIBLE_DATE_ALTERNATIVES_MODE = "off"
    try:
        session.turn = 1
        # Pinned `now`, per the fixtures convention: the captured payload's dates are in
        # 2026-06, which real wall-clock time has since passed, and build_quote's
        # real-time floor (REVIEW_QUEUE #17) correctly rejects them.
        extend_flow.build_quote(session, "2026-06-17", now=EXTEND_TEST_NOW)
        session.turn = 2
    finally:
        extend_flow.quote_change = original
        config.FLEXIBLE_DATE_ALTERNATIVES_MODE = original_mode


@deferred(
    "DELIBERATE: reading 'no' out of free text is intent classification, and putting that "
    "in the money layer is the LLM-touching-money pattern the architecture exists to "
    "prevent. Answered in DECISIONS.md §3 rather than implemented."
)
def test_later_turn_without_affirmative_consent_cannot_extend():
    """A turn boundary proves the quote was visible, not that the answer was yes."""
    session = _session()
    _stage_quote(session)
    session.record("customer", "No, do not extend it.")

    writes = []
    original = extend_flow.extend_reservation
    extend_flow.extend_reservation = lambda *args, **kwargs: (
        writes.append(args),
        {"success": True, "confirmation_number": "EXT-UNWANTED", "charges": QUOTE["quote"]["charges"]},
    )[1]
    try:
        outcome = extend_flow.commit_extension(
            session, "marcus.lee@example.com", "123", "60601"
        )
    finally:
        extend_flow.extend_reservation = original

    assert writes == [], f"a negative customer reply still authorized a write: {outcome}"
    assert outcome["ok"] is False, outcome


def test_repriced_quote_requires_another_customer_turn():
    """A new total returned during this turn must not be chargeable in the same turn."""
    session = _session()
    _stage_quote(session)
    session.pending_quote.total_charged = 99.99

    original_ttl = config.QUOTE_TTL_SECONDS
    original_quote = extend_flow.quote_change
    original_write = extend_flow.extend_reservation
    writes = []
    config.QUOTE_TTL_SECONDS = 0
    extend_flow.quote_change = lambda *args, **kwargs: QUOTE
    extend_flow.extend_reservation = lambda *args, **kwargs: (
        writes.append(args),
        {"success": True, "confirmation_number": "EXT-REPRICED", "charges": QUOTE["quote"]["charges"]},
    )[1]
    try:
        first = extend_flow.commit_extension(
            session, "marcus.lee@example.com", "123", "60601"
        )
        second = extend_flow.commit_extension(
            session, "marcus.lee@example.com", "123", "60601"
        )
    finally:
        config.QUOTE_TTL_SECONDS = original_ttl
        extend_flow.quote_change = original_quote
        extend_flow.extend_reservation = original_write

    assert first.get("price_changed") is True, first
    assert writes == [], f"repriced total was charged before another customer turn: {second}"
    assert second["ok"] is False, second


def test_extension_write_total_must_be_reconciled_with_quote():
    """The write response must flag a charge larger than the accepted quote."""
    session = _session()
    _stage_quote(session)

    actual_charges = dict(QUOTE["quote"]["charges"], total_charged=199.99)
    original = extend_flow.extend_reservation
    extend_flow.extend_reservation = lambda *args, **kwargs: {
        "success": True,
        "confirmation_number": "EXT-HIGHER-TOTAL",
        "charges": actual_charges,
    }
    try:
        outcome = extend_flow.commit_extension(
            session, "marcus.lee@example.com", "123", "60601"
        )
    finally:
        extend_flow.extend_reservation = original

    assert outcome.get("price_changed") or outcome.get("escalate"), (
        f"write charged {actual_charges['total_charged']} after quote "
        f"{session.pending_quote.total_charged}, but no mismatch was flagged: {outcome}"
    )


def test_in_rental_cancel_requires_resolved_disambiguation():
    """Choosing early return must not leave a staged cancellation committable."""
    session = _session()  # captured reservation has pickup in the past
    session.turn = 1
    summary = cancel_flow.build_cancel_estimate(session)
    assert summary.get("requires_disambiguation") is True, summary
    session.turn = 2
    session.record("customer", "I mean return it early; do not cancel the reservation.")

    writes = []
    original = cancel_flow.cancel_reservation
    cancel_flow.cancel_reservation = lambda *args, **kwargs: (
        writes.append(args),
        {
            "success": True,
            "confirmation_number": "CXL-UNWANTED",
            "cancellation_details": {
                "penalty": 38.99,
                "refund_amount": 77.98,
                "prepaid_amount": 116.97,
                "currency": "USD",
            },
        },
    )[1]
    try:
        outcome = cancel_flow.commit_cancellation(
            session, "marcus.lee@example.com"
        )
    finally:
        cancel_flow.cancel_reservation = original

    assert writes == [], f"early-return intent still authorized cancellation: {outcome}"
    assert outcome["ok"] is False, outcome


def test_http_200_failed_envelope_is_not_treated_as_success():
    """HTTP status alone is insufficient when the documented body says success=false."""
    response = FakeResponse(
        200,
        error_body("PAYMENT_DECLINED", "card declined despite 200 response"),
    )
    with fake_transport([response]):
        try:
            avis_client.cancel_reservation(
                "AVS-48372915", "marcus.lee@example.com"
            )
        except AvisAPIError as exc:
            assert exc.code == "PAYMENT_DECLINED", exc.code
            return
    raise AssertionError("a success=false envelope returned normally because HTTP was 200")


def test_incomplete_extension_success_is_not_consumed():
    """A write missing confirmation and charges must not become a completed extension."""
    session = _session()
    _stage_quote(session)

    original = extend_flow.extend_reservation
    extend_flow.extend_reservation = lambda *args, **kwargs: {"success": True}
    try:
        outcome = extend_flow.commit_extension(
            session, "marcus.lee@example.com", "123", "60601"
        )
    finally:
        extend_flow.extend_reservation = original

    assert outcome["ok"] is False, f"incomplete write was reported successful: {outcome}"
    assert session.pending_quote.consumed is False, "incomplete write consumed the quote"


@deferred(
    "DELIBERATE: satisfying this means redacting every bare 3-4 digit string — dates, "
    "totals, ZIP fragments — from the transcript a human needs to read. The contextual "
    "fix (scrub the turn following a CVV prompt) needs a prompt/answer pairing the "
    "session does not model yet. DECISIONS.md §3."
)
def test_bare_cvv_is_scrubbed_from_handoff_transcript():
    """Customers commonly answer a CVV prompt with only the digits."""
    assert "847" not in redact_text("847"), "a bare CVV survives transcript redaction"


@deferred(
    "DELIBERATE: needs a branch-code -> IANA timezone map the reservation payload does "
    "not carry. Worst case is a one-hour error across a DST boundary; no money gate "
    "depends on it. DECISIONS.md §5, timezones."
)
def test_branch_local_time_uses_target_dates_dst_offset():
    """ORD is UTC-5 in June but UTC-6 in November; wall time must remain Chicago-local."""
    session = _session()
    normalized = extend_flow.normalize_datetime("2026-11-10T10:00", session)
    expected = datetime(2026, 11, 10, 10, 0, tzinfo=ZoneInfo("America/Chicago")).isoformat()
    assert normalized == expected, f"expected branch-local {expected}, got {normalized}"


def test_handoff_tool_does_not_say_supported_cancel_is_forbidden():
    """Tool descriptions are model-visible and must agree with the main instructions."""
    description = agent.escalate_to_human.description.lower()
    assert "must not take (cancel" not in description, description


def test_uncovered_topic_framed_as_a_policy_question_returns_nothing():
    """A deterministic false positive leaves the model to invent the safety boundary."""
    query = "what is your pet policy"
    results = kb.search(query)
    assert results == [], f"{query!r} returned unrelated articles: {[r['id'] for r in results]}"


@deferred(
    "DELIBERATE: lexically indistinguishable from a legitimate query. 'insurance or "
    "damage waiver' (1 known token, 3 unknown, single title hit) has the identical shape "
    "to 'how do I get my money back if I cancel' — any rule that drops the first drops "
    "the second. Mitigated instead by low_confidence on thin matches. DECISIONS.md §2."
)
def test_uncovered_topic_sharing_a_word_with_an_article_title_returns_nothing():
    query = "do you cover insurance or a damage waiver"
    results = kb.search(query)
    assert results == [], f"{query!r} returned unrelated articles: {[r['id'] for r in results]}"


def test_thin_single_token_matches_are_flagged_low_confidence():
    """What the deferred case above is mitigated with: say so instead of silently guessing."""
    hits = kb.search("do you cover insurance or a damage waiver")
    assert hits, "expected the thin match to still be returned, flagged"
    assert all(h["low_confidence"] for h in hits), hits
    assert not any(h["low_confidence"] for h in kb.search("grace period late return"))


if __name__ == "__main__":
    # No bare runner here, unlike the older test files: the deferred cases carry pytest
    # markers, and a hand-rolled loop would report a documented, deliberate gap as a
    # failure. Run this file through pytest.
    raise SystemExit("Run with: python -m pytest tests/test_additional_safety.py")
