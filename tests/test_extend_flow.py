"""Extend-flow logic tests: datetime handling and the confirmation gate.

No network, no LLM — the client is faked so the gate can be exercised deterministically.
Run: python tests/test_extend_flow.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import avis_client  # noqa: E402
import extend_flow  # noqa: E402
from fixtures import EXTEND_TEST_NOW, reservation  # noqa: E402
from session import ServicingSession  # noqa: E402

avis_client.BACKOFF_BASE_S = 0

QUOTE = {"success": True, "quote": {"charges": {
    "daily_rate": 38.99, "extension_days": 3, "subtotal": 116.97, "late_fee": 29.0,
    "one_way_fee": 0.0, "taxes_and_fees": 13.5, "total_charged": 159.47, "currency": "USD"}}}


def _session():
    s = ServicingSession()
    s.load_reservation(reservation())
    return s


def _quote_then_next_turn(session, when="2026-06-17"):
    """Quote on one turn, then advance — the write gate requires a turn boundary, which
    is what "the customer saw the total and replied" means structurally (audit #3)."""
    session.turn = 1
    extend_flow.build_quote(session, when, now=EXTEND_TEST_NOW)
    session.turn = 2
    return session


def _with_stubs(quote=None, write=None):
    """Swap the client functions extend_flow calls; returns a restore callable."""
    orig_q, orig_w = extend_flow.quote_change, extend_flow.extend_reservation
    extend_flow.quote_change = quote or (lambda *a, **k: QUOTE)
    extend_flow.extend_reservation = write or (
        lambda *a, **k: {"success": True, "confirmation_number": "EXT-TEST"})
    return lambda: (setattr(extend_flow, "quote_change", orig_q),
                    setattr(extend_flow, "extend_reservation", orig_w))


def test_unparseable_datetimes_raise_flowerror_not_valueerror():
    """The model may pass a customer's words straight through ('June 17th'). That must be
    recoverable by re-asking, never an exception escaping the tool."""
    restore = _with_stubs()
    try:
        session = _session()
        for raw in ["June 17th", "next friday", "2026-13-45", "17/06/2026",
                    "2026-06-17T25:00", "2026-02-30", "", "tomorrow"]:
            try:
                extend_flow.build_quote(session, raw, now=EXTEND_TEST_NOW)
                raise AssertionError(f"{raw!r} was accepted as a datetime")
            except extend_flow.FlowError:
                pass
            except Exception as exc:
                raise AssertionError(f"{raw!r} raised {type(exc).__name__}, not FlowError")
    finally:
        restore()


def test_valid_datetime_forms_normalize_to_branch_offset():
    restore = _with_stubs()
    try:
        session = _session()
        offset = session.return_utc_offset
        for raw in ["2026-06-17", "2026-06-17T15:00", f"2026-06-17T15:00:00{offset}"]:
            out = extend_flow.build_quote(session, raw, now=EXTEND_TEST_NOW)
            assert out["new_return_datetime"].endswith(offset), out["new_return_datetime"]
    finally:
        restore()


def test_extension_must_move_return_later():
    restore = _with_stubs()
    try:
        session = _session()
        try:
            extend_flow.build_quote(session, "2026-06-01", now=EXTEND_TEST_NOW)  # before current return
            raise AssertionError("a backwards extension was quoted")
        except extend_flow.FlowError as exc:
            assert "not after" in str(exc)
    finally:
        restore()


def test_no_charge_without_a_quote_the_customer_saw():
    """The confirmation gate. A model told 'just charge it' still cannot."""
    restore = _with_stubs()
    try:
        session = _session()
        out = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert out["ok"] is False and "quote" in out["customer_message"].lower(), out
    finally:
        restore()


def test_write_allowed_once_a_quote_is_staged():
    restore = _with_stubs()
    try:
        session = _quote_then_next_turn(_session())
        out = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert out["ok"] is True and out["confirmation_number"] == "EXT-TEST", out
    finally:
        restore()


def test_price_move_blocks_the_write_and_asks_again():
    """Quote staleness: the customer is never charged a number they didn't accept."""
    import config
    original_ttl = config.QUOTE_TTL_SECONDS
    config.QUOTE_TTL_SECONDS = 0
    restore = _with_stubs()
    try:
        session = _quote_then_next_turn(_session())
        session.pending_quote.total_charged = 99.99  # pretend we quoted something else
        out = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert out["ok"] is False and out.get("price_changed") is True, out
        assert out["previous_total"] == 99.99 and out["new_total"] == 159.47, out
    finally:
        config.QUOTE_TTL_SECONDS = original_ttl
        restore()


# --- REVIEW_QUEUE #17: real-time floor -----------------------------------------------

def test_target_after_stale_return_but_before_real_now_is_rejected():
    """The exact live bug: 'extend to June 20' accepted because June 20 is after the
    reservation's stale June-14 return date, even though June 20 had already passed
    relative to real time. The reservation's own dates are not a substitute for
    knowing what day it actually is."""
    restore = _with_stubs()
    try:
        session = _session()  # current_return_datetime ~= 2026-06-09
        real_now = datetime(2026, 8, 6, tzinfo=timezone.utc)
        try:
            extend_flow.build_quote(session, "2026-06-20", now=real_now)
            raise AssertionError("a target already past relative to real time was accepted")
        except extend_flow.FlowError as exc:
            assert "already passed" in str(exc), exc
        assert session.pending_quote is None
    finally:
        restore()


def test_target_after_real_now_is_accepted():
    restore = _with_stubs()
    try:
        session = _session()
        real_now = datetime(2026, 8, 6, tzinfo=timezone.utc)
        out = extend_flow.build_quote(session, "2026-08-10", now=real_now)
        assert out["total_charged"] is not None
    finally:
        restore()


def test_real_now_defaults_to_actual_wall_clock():
    """No `now` passed -> the guard must use the real system clock. A target after the
    reservation's own stale return date (so it clears THAT check) but still in the past
    relative to real 'today' must be rejected without any test injection -- this is the
    live bug reproduced with zero mocking of time."""
    assert datetime.now(timezone.utc) > datetime(2026, 7, 1, tzinfo=timezone.utc), \
        "test assumption stale: real time is no longer safely after this fixture's dates"
    restore = _with_stubs()
    try:
        session = _session()  # current_return_datetime ~= 2026-06-09
        try:
            extend_flow.build_quote(session, "2026-06-20")  # no now= override
            raise AssertionError("a target already past relative to real time was accepted")
        except extend_flow.FlowError as exc:
            assert "already passed" in str(exc), exc
    finally:
        restore()


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
