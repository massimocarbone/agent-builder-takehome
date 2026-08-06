"""Regression tests for the 2026-08-06 audit findings.

Each test reproduces the specific defect the audit described, so a regression fails here
rather than in a customer conversation. Run: python tests/test_audit_fixes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import avis_client  # noqa: E402
import config  # noqa: E402
import extend_flow  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from fixtures import EXTEND_TEST_NOW, reservation  # noqa: E402
from session import ServicingSession  # noqa: E402

avis_client.BACKOFF_BASE_S = 0

QUOTE = {"success": True, "quote": {"charges": {
    "daily_rate": 38.99, "extension_days": 3, "subtotal": 116.97, "late_fee": 29.0,
    "one_way_fee": 0.0, "taxes_and_fees": 13.5, "total_charged": 159.47, "currency": "USD"}}}

# Mirrors the documented extend response shape (confirmation number + extension details +
# charges), not the abbreviated stub this file started with.
WRITE_OK = {"success": True, "confirmation_number": "EXT-TEST",
            "extension_details": {"extension_days": 3, "late_return": True},
            "charges": QUOTE["quote"]["charges"]}


class Ctx:
    def __init__(self, session):
        self.context = session


def _stub_client(quote=None, write=None):
    orig = (extend_flow.quote_change, extend_flow.extend_reservation)
    extend_flow.quote_change = quote or (lambda *a, **k: QUOTE)
    extend_flow.extend_reservation = write or (lambda *a, **k: WRITE_OK)
    return lambda: (setattr(extend_flow, "quote_change", orig[0]),
                    setattr(extend_flow, "extend_reservation", orig[1]))


def _staged(session, turn_now=2):
    """A session with a quote the customer has 'seen' (staged on an earlier turn)."""
    session.turn = 1
    extend_flow.build_quote(session, "2026-06-17", now=EXTEND_TEST_NOW)
    session.turn = turn_now
    return session


def _session(base="standard"):
    s = ServicingSession()
    s.load_reservation(reservation(base))
    return s


# --- #1 reservation switch clears reservation-scoped state --------------------------

def test_switching_reservation_clears_verification():
    """Proving you know reservation A's email must not unlock reservation B's card."""
    session = _session()
    session.verified_email = "marcus.lee@example.com"
    calls = []
    orig = avis_client.get_reservation
    avis_client.get_reservation = lambda rid: (calls.append(rid),
                                               reservation("preferred"))[1]
    try:
        out = _call = agent.lookup_reservation.__wrapped__(
            Ctx(session), reservation_id="AVS-29471835")
    finally:
        avis_client.get_reservation = orig
    assert calls, "stub never intercepted — test was hitting the real API"
    assert session.verified_email is None, "verification survived a reservation switch"
    assert out["card_last_four"] == "withheld until verified", out["card_last_four"]


def test_switching_reservation_discards_staged_quote():
    """A quote priced against A must not be committable against B."""
    restore = _stub_client()
    try:
        session = _staged(_session())
        assert session.pending_quote is not None
        session.load_reservation(reservation("preferred"))
        assert session.pending_quote is None, "quote survived a reservation switch"
        assert session.failed_verifications == 0
    finally:
        restore()


def test_reloading_same_reservation_preserves_state():
    """Re-reading the same booking is not a switch — verification must survive."""
    session = _session()
    session.verified_email = "marcus.lee@example.com"
    session.load_reservation(reservation())
    assert session.verified_email == "marcus.lee@example.com"


def test_switch_does_not_reopen_a_handed_off_session():
    session = _session()
    session.handed_off = True
    session.load_reservation(reservation("preferred"))
    assert session.handed_off is True, "naming another reservation reopened a handoff"


# --- #2 a consumed quote cannot be charged twice ------------------------------------

def test_quote_cannot_be_charged_twice():
    """Each write mints a fresh idempotency key, so the API's replay protection does not
    cover a repeated tool call — only refusing here does."""
    writes = []
    restore = _stub_client(write=lambda *a, **k: (writes.append(1), WRITE_OK)[1])
    try:
        session = _staged(_session())
        first = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert first["ok"] is True and len(writes) == 1
        second = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert second["ok"] is False and second.get("already_processed") is True, second
        assert len(writes) == 1, f"charged {len(writes)} times"
    finally:
        restore()


# --- #3 consent requires a turn boundary --------------------------------------------

def test_quote_and_charge_in_one_turn_is_refused():
    """The model can call both tools in a single run, before the customer has seen a
    single word of the quote. That is not consent."""
    restore = _stub_client()
    try:
        session = _session()
        session.turn = 1
        extend_flow.build_quote(session, "2026-06-17", now=EXTEND_TEST_NOW)
        out = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert out["ok"] is False, "charged within the same turn the quote was created"
        assert "not seen" in out["customer_message"], out
    finally:
        restore()


def test_charge_allowed_on_a_later_turn():
    restore = _stub_client()
    try:
        out = extend_flow.commit_extension(_staged(_session()),
                                           "marcus.lee@example.com", "123", "60601")
        assert out["ok"] is True, out
    finally:
        restore()


# --- #4 incomplete quote responses are rejected -------------------------------------

def test_quote_without_total_is_rejected():
    """Staging 0.0 and handing the model None would let a customer confirm an amount
    nobody ever stated."""
    incomplete = {"success": True, "quote": {"charges": {"daily_rate": 38.99}}}
    restore = _stub_client(quote=lambda *a, **k: incomplete)
    try:
        session = _session()
        try:
            extend_flow.build_quote(session, "2026-06-17", now=EXTEND_TEST_NOW)
            raise AssertionError("a quote with no total was accepted")
        except extend_flow.FlowError:
            pass
        assert session.pending_quote is None, "an unpriced quote was staged"
    finally:
        restore()


# --- #5/#6 terminal failures set the terminal state ---------------------------------

def test_terminal_api_failure_hands_off():
    session = _session()
    for code in ("PAYMENT_DECLINED", "EXHAUSTED_RETRIES", "MALFORMED_RESERVATION"):
        session.handed_off = False
        out = extend_flow.classify_failure(
            session, AvisAPIError(code, code, retryable=False), "confirm_extension")
        assert out["escalate"] is True, code
        assert session.handed_off is True, f"{code} escalated but left tools usable"


def test_verification_lockout_hands_off_and_blocks_further_attempts():
    def always_403(*a, **k):
        raise AvisAPIError("VERIFICATION_FAILED", "nope", status=403)

    restore = _stub_client(write=always_403)
    try:
        session = _staged(_session())
        for _ in range(config.MAX_VERIFICATION_ATTEMPTS):
            out = extend_flow.commit_extension(session, "wrong@example.com", "123", "60601")
        assert out.get("escalate") is True, out
        assert session.handed_off is True, "lockout announced but session still open"
        assert agent._blocked(session) is not None, "tools usable after lockout"
    finally:
        restore()


# --- #7 unparseable stored datetimes are caught at the boundary ---------------------

def test_unparseable_stored_datetime_rejected_at_boundary():
    """The customer-input guard already covers 'June 17th'; stored data deserves it too."""
    for field in ("pickup_datetime", "current_return_datetime"):
        broken = reservation(**{f"dates.{field}": "not-a-date"})
        try:
            avis_client.validate_reservation(broken)
            raise AssertionError(f"unparseable {field} accepted")
        except AvisAPIError as exc:
            assert exc.code == "MALFORMED_RESERVATION", exc.code
            assert any(field in m for m in exc.details["missing_fields"]), exc.details


def test_valid_stored_datetimes_still_pass():
    avis_client.validate_reservation(reservation())


# --- #9 speculative quotes are time-bounded -----------------------------------------

def test_slow_alternatives_do_not_block_the_primary_quote():
    """The nicety must never delay the extension the customer actually asked for."""
    import time as _time
    original_mode, original_budget = (config.FLEXIBLE_DATE_ALTERNATIVES_MODE,
                                      config.ALTERNATIVE_QUOTE_BUDGET_S)
    config.FLEXIBLE_DATE_ALTERNATIVES_MODE = "shadow"
    config.ALTERNATIVE_QUOTE_BUDGET_S = 0.5

    def slow_quote(reservation_id, change_type, dt, *a, **k):
        if dt.startswith("2026-06-17"):
            return QUOTE                      # primary: fast
        _time.sleep(10)                       # speculative: hangs
        return QUOTE

    restore = _stub_client(quote=slow_quote)
    try:
        session = _session()
        started = _time.monotonic()
        out = extend_flow.build_quote(session, "2026-06-17", now=EXTEND_TEST_NOW)
        elapsed = _time.monotonic() - started
        assert out["total_charged"] == 159.47, out
        assert elapsed < 3, f"primary quote blocked {elapsed:.1f}s on speculative work"
    finally:
        config.FLEXIBLE_DATE_ALTERNATIVES_MODE = original_mode
        config.ALTERNATIVE_QUOTE_BUDGET_S = original_budget
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
