"""Property tests for the money- and trust-sensitive deterministic layers.

These tests complement the named examples in the rest of the suite.  They generate
small, reproducible variations of captured payloads and scripted transports; they do
not call the live Avis API or an LLM.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import avis_client  # noqa: E402
import cancel_flow  # noqa: E402
import config  # noqa: E402
import extend_flow  # noqa: E402
import policy  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from fixtures import (  # noqa: E402
    DELETE,
    ConnectionError_,
    FakeResponse,
    Timeout,
    error_body,
    fake_transport,
    reservation,
)
from property_strategies import (  # noqa: E402
    money_cents,
    positive_money_cents,
    required_reservation_paths,
    transient_prefixes,
)
from session import PendingCancellation, ServicingSession  # noqa: E402


# These are unit-level properties over pure functions or in-memory fake transports.
# A small deterministic budget gives CI broad boundary coverage without making a
# take-home test suite slow or allowing timing to influence which examples are tried.
property_test = settings(
    max_examples=30,
    deadline=None,
    derandomize=True,
    suppress_health_check=(HealthCheck.too_slow,),
)


# --- API boundary validation -------------------------------------------------------


@property_test
@given(path=required_reservation_paths)
@example(path="dates.current_return_datetime")
def test_every_required_reservation_field_is_rejected_when_absent(path: str) -> None:
    """One missing required field must become a classified terminal error."""
    payload = reservation(**{path: DELETE})

    try:
        avis_client.validate_reservation(payload)
    except AvisAPIError as exc:
        assert exc.code == "MALFORMED_RESERVATION", (
            f"missing {path!r} was classified as {exc.code!r}"
        )
        assert exc.retryable is False, f"malformed local data was marked retryable: {path}"
        assert path in exc.details["missing_fields"], (
            f"error for missing {path!r} reported {exc.details['missing_fields']!r}"
        )
    else:
        raise AssertionError(f"captured reservation was accepted after deleting {path!r}")


@property_test
@given(field=st.sampled_from(("pickup_datetime", "current_return_datetime")),
       malformed=st.sampled_from(("", "tomorrow", "2026-13-45", "2026-06-17T25:00")))
@example(field="current_return_datetime", malformed="not-an-iso-datetime")
def test_malformed_stored_datetimes_never_reach_workflow_code(
    field: str, malformed: str,
) -> None:
    payload = reservation(**{f"dates.{field}": malformed})

    try:
        avis_client.validate_reservation(payload)
    except AvisAPIError as exc:
        reported = exc.details["missing_fields"]
        assert exc.code == "MALFORMED_RESERVATION"
        assert any(item.startswith(f"dates.{field} (unparseable:") for item in reported), (
            f"malformed dates.{field}={malformed!r} was reported as {reported!r}"
        )
    else:
        raise AssertionError(f"unparseable dates.{field}={malformed!r} was accepted")


# --- Datetime normalization and quote boundaries ----------------------------------


@property_test
@given(target_date=st.dates(min_value=date(2026, 1, 1), max_value=date(2032, 12, 31)))
@example(target_date=date(2026, 11, 10))
def test_date_only_return_preserves_captured_branch_time_and_offset(target_date: date) -> None:
    session = ServicingSession()
    session.load_reservation(reservation())
    current = session.reservation["dates"]["current_return_datetime"]

    normalized = extend_flow.normalize_datetime(target_date.isoformat(), session)

    assert datetime.fromisoformat(normalized).date() == target_date
    assert normalized[11:19] == current[11:19], (
        f"date-only input changed branch return time: {current!r} -> {normalized!r}"
    )
    assert normalized[-6:] == session.return_utc_offset, (
        f"date-only input lost branch offset: {normalized!r}"
    )


@property_test
@given(target_date=st.dates(min_value=date(2026, 1, 1), max_value=date(2032, 12, 31)),
       target_time=st.builds(time, hour=st.integers(0, 23), minute=st.integers(0, 59)))
@example(target_date=date(2026, 6, 17), target_time=time(0, 0))
@example(target_date=date(2026, 6, 17), target_time=time(23, 59))
def test_naive_minute_datetime_is_interpreted_at_the_branch(
    target_date: date, target_time: time,
) -> None:
    session = ServicingSession()
    session.load_reservation(reservation())
    raw = f"{target_date.isoformat()}T{target_time.hour:02d}:{target_time.minute:02d}"

    normalized = extend_flow.normalize_datetime(raw, session)

    parsed = datetime.fromisoformat(normalized)
    assert parsed.replace(tzinfo=None) == datetime.combine(target_date, target_time)
    assert normalized.endswith(session.return_utc_offset), (
        f"naive customer time was not grounded at the branch: {raw!r} -> {normalized!r}"
    )


@property_test
@given(minutes_from_current=st.integers(min_value=-10_000, max_value=0))
@example(minutes_from_current=0)
@example(minutes_from_current=-1)
def test_quote_refuses_every_target_at_or_before_current_return(
    minutes_from_current: int,
) -> None:
    session = ServicingSession()
    session.load_reservation(reservation())
    current = datetime.fromisoformat(session.reservation["dates"]["current_return_datetime"])
    target = current + timedelta(minutes=minutes_from_current)

    try:
        extend_flow.build_quote(session, target.isoformat(), now=datetime(2020, 1, 1,
                                                                         tzinfo=timezone.utc))
    except extend_flow.FlowError as exc:
        assert "must move the return later" in str(exc), (
            f"unexpected refusal for {target.isoformat()}: {exc}"
        )
        assert session.pending_quote is None
    else:
        raise AssertionError(
            f"quote accepted target {minutes_from_current} minutes from current return"
        )


# --- Cancellation policy and post-write threshold ---------------------------------


POLICY_NOW = datetime(2030, 1, 15, 12, 0, tzinfo=timezone.utc)


@property_test
@given(hours_until_pickup=st.integers(min_value=-240, max_value=240),
       daily_rate_cents=positive_money_cents,
       prepaid_cents=money_cents)
@example(hours_until_pickup=48, daily_rate_cents=5_000, prepaid_cents=3_000)
@example(hours_until_pickup=49, daily_rate_cents=5_000, prepaid_cents=3_000)
@example(hours_until_pickup=0, daily_rate_cents=3_899, prepaid_cents=11_697)
def test_cancel_estimate_conserves_prepaid_amount(
    hours_until_pickup: int, daily_rate_cents: int, prepaid_cents: int,
) -> None:
    pickup = POLICY_NOW + timedelta(hours=hours_until_pickup)
    payload = reservation(**{
        "dates.pickup_datetime": pickup.isoformat(),
        "pricing.daily_rate": daily_rate_cents / 100,
        "payment.total_charged": prepaid_cents / 100,
    })

    estimate = policy.compute_cancel_estimate(payload, now=POLICY_NOW)

    assert estimate.penalty >= 0, f"negative penalty for {payload!r}"
    assert estimate.refund >= 0, f"negative refund for {payload!r}"
    assert estimate.penalty <= estimate.prepaid, (
        f"penalty {estimate.penalty} exceeded prepaid {estimate.prepaid}"
    )
    assert round(estimate.penalty + estimate.refund, 2) == estimate.prepaid, (
        f"estimate failed conservation: penalty={estimate.penalty}, "
        f"refund={estimate.refund}, prepaid={estimate.prepaid}"
    )

    expected_branch = (
        policy.PRE_PICKUP_FREE if hours_until_pickup > 48
        else policy.PRE_PICKUP_PENALTY if hours_until_pickup > 0
        else policy.IN_RENTAL
    )
    if prepaid_cents == 0 and hours_until_pickup > 0:
        # Pay-at-counter has no monetary penalty and deliberately collapses both
        # pre-pickup windows into the free branch.
        expected_branch = policy.PRE_PICKUP_FREE
    assert estimate.branch == expected_branch, (
        f"{hours_until_pickup}h from pickup chose {estimate.branch}, expected {expected_branch}"
    )
    assert estimate.requires_disambiguation is (hours_until_pickup <= 0)


def _cancellation_at_delta(delta: float) -> tuple[dict, ServicingSession]:
    """Run the real cancellation reconciliation against a deterministic fake write."""
    payload = reservation()
    session = ServicingSession()
    session.load_reservation(payload)
    session.turn = 2
    estimated_penalty = 40.0
    estimated_refund = 80.0
    session.pending_cancellation = PendingCancellation(
        branch=policy.PRE_PICKUP_PENALTY,
        penalty_estimate=estimated_penalty,
        refund_estimate=estimated_refund,
        prepaid=120.0,
        currency="USD",
        citation=dict(policy.CITATION),
        caveats=["test estimate"],
        quoted_on_turn=1,
    )

    original = cancel_flow.cancel_reservation
    cancel_flow.cancel_reservation = lambda *args, **kwargs: {
        "success": True,
        "confirmation_number": "CXL-PROPERTY",
        "cancellation_details": {
            "penalty": estimated_penalty + delta,
            "refund_amount": estimated_refund,
            "prepaid_amount": 120.0,
            "currency": "USD",
        },
    }
    try:
        return cancel_flow.commit_cancellation(session, "customer@example.com"), session
    finally:
        cancel_flow.cancel_reservation = original


@property_test
@given(delta_cents=st.integers(min_value=-500, max_value=500))
@example(delta_cents=99)
@example(delta_cents=100)
@example(delta_cents=-100)
def test_cancel_variance_threshold_is_inclusive_and_directional(delta_cents: int) -> None:
    delta = delta_cents / 100
    outcome, session = _cancellation_at_delta(delta)
    threshold = config.CANCEL_VARIANCE_THRESHOLD_USD

    should_escalate = delta >= threshold
    assert outcome.get("escalate", False) is should_escalate, (
        f"delta={delta:.2f}, threshold={threshold:.2f}, outcome={outcome!r}"
    )
    assert session.handed_off is should_escalate, (
        f"handoff disagreed with adverse threshold for delta={delta:.2f}"
    )
    if delta <= -threshold:
        assert outcome.get("better_than_estimate") is True, (
            f"favorable delta={delta:.2f} was not identified: {outcome!r}"
        )


# --- Retry and idempotency contract ------------------------------------------------


def _transient_step(kind: str):
    if kind == "timeout":
        return Timeout
    if kind == "connection":
        return ConnectionError_
    status = int(kind.removeprefix("http_"))
    return FakeResponse(status, error_body(f"HTTP_{status}"))


@property_test
@given(prefix=transient_prefixes)
@example(prefix=["timeout", "connection", "http_503"])
def test_write_retries_are_bounded_and_reuse_one_idempotency_key(prefix: list[str]) -> None:
    success = FakeResponse(200, {"success": True, "confirmation_number": "CXL-PROPERTY"})
    script = [_transient_step(kind) for kind in prefix] + [success]
    original_backoff = avis_client.BACKOFF_BASE_S
    avis_client.BACKOFF_BASE_S = 0
    try:
        with fake_transport(script) as calls:
            result = avis_client.cancel_reservation("AVS-48372915", "customer@example.com")
    finally:
        avis_client.BACKOFF_BASE_S = original_backoff

    assert result["confirmation_number"] == "CXL-PROPERTY"
    assert len(calls) == len(prefix) + 1 <= avis_client.MAX_ATTEMPTS
    keys = [call["headers"].get("Idempotency-Key") for call in calls]
    assert keys[0] is not None, "write was sent without an idempotency key"
    assert len(set(keys)) == 1, f"one logical write used different keys: {keys!r}"


@property_test
@given(prefix=transient_prefixes,
       terminal_status=st.sampled_from((400, 401, 403, 404, 409, 422, 429)))
@example(prefix=["timeout", "http_500"], terminal_status=409)
def test_terminal_client_error_stops_retrying_after_any_transient_prefix(
    prefix: list[str], terminal_status: int,
) -> None:
    # Leave room for the terminal response; otherwise the client correctly exhausts its
    # retry budget before it can observe that response.
    prefix = prefix[: avis_client.MAX_ATTEMPTS - 1]
    terminal = FakeResponse(terminal_status, error_body(f"TERMINAL_{terminal_status}"))
    script = [_transient_step(kind) for kind in prefix] + [terminal]
    original_backoff = avis_client.BACKOFF_BASE_S
    avis_client.BACKOFF_BASE_S = 0
    try:
        with fake_transport(script) as calls:
            try:
                avis_client.cancel_reservation("AVS-48372915", "customer@example.com")
            except AvisAPIError as exc:
                assert exc.code == f"TERMINAL_{terminal_status}"
                assert exc.retryable is False
            else:
                raise AssertionError(f"terminal HTTP {terminal_status} was accepted")
    finally:
        avis_client.BACKOFF_BASE_S = original_backoff

    assert len(calls) == len(prefix) + 1, (
        f"terminal HTTP {terminal_status} was retried; calls={calls!r}"
    )
    keys = {call["headers"].get("Idempotency-Key") for call in calls}
    assert len(keys) == 1 and None not in keys, (
        f"idempotency key changed before terminal error: {keys!r}"
    )
