"""Extend-flow logic tests: datetime handling and the confirmation gate.

No network, no LLM — the client is faked so the gate can be exercised deterministically.
Run: python tests/test_extend_flow.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import avis_client  # noqa: E402
import extend_flow  # noqa: E402
from fixtures import reservation  # noqa: E402
from session import ServicingSession  # noqa: E402

avis_client.BACKOFF_BASE_S = 0

QUOTE = {"success": True, "quote": {"charges": {
    "daily_rate": 38.99, "extension_days": 3, "subtotal": 116.97, "late_fee": 29.0,
    "one_way_fee": 0.0, "taxes_and_fees": 13.5, "total_charged": 159.47, "currency": "USD"}}}


def _session():
    s = ServicingSession()
    s.reservation = reservation()
    return s


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
                extend_flow.build_quote(session, raw)
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
            out = extend_flow.build_quote(session, raw)
            assert out["new_return_datetime"].endswith(offset), out["new_return_datetime"]
    finally:
        restore()


def test_extension_must_move_return_later():
    restore = _with_stubs()
    try:
        session = _session()
        try:
            extend_flow.build_quote(session, "2026-06-01")  # before current return
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
        session = _session()
        extend_flow.build_quote(session, "2026-06-17")
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
        session = _session()
        extend_flow.build_quote(session, "2026-06-17")
        session.pending_quote.total_charged = 99.99  # pretend we quoted something else
        out = extend_flow.commit_extension(session, "marcus.lee@example.com", "123", "60601")
        assert out["ok"] is False and out.get("price_changed") is True, out
        assert out["previous_total"] == 99.99 and out["new_total"] == 159.47, out
    finally:
        config.QUOTE_TTL_SECONDS = original_ttl
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
