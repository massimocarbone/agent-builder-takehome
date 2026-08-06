"""An offline stand-in for the Avis API, built from one loaded reservation payload.

Not a client-level mock (tests/fixtures.py's ``fake_transport`` already does that, at
the HTTP seam, for testing ``avis_client.py`` itself — see its docstring on the two
deliberately separate seams). This is the *flow*-level seam: it replaces the client
functions that ``extend_flow``, ``cancel_flow``, and ``agent.py`` call directly, so the
real agent, the real prompt, and the real gates run against data you control instead of
the live mock API.

**Pricing is reverse-engineered, not authoritative.** The real API's exact formula is
undocumented; this reproduces what's observable from captured examples and the API
reference's own worked example:
  - subtotal = daily_rate * extension_days (partial days round up — kb_ext_03)
  - late_fee = a flat $29 (config.LATE_RETURN_FEE_USD) when the reservation's current
    return time has already passed real time AND the member is standard — Preferred is
    exempt. Every captured example with a future return date carries late_fee: 0.0.
  - one_way_fee = a flat placeholder when the return location differs from pickup
    (untested against the real API — no captured example changes location)
  - taxes_and_fees = 9.25% of (subtotal + late_fee + one_way_fee), the rate implied by
    every captured quote/extend example (8.51/91.98, 13.50/145.97 both land at 9.25%)
Good enough to exercise the CONVERSATION and the GATES faithfully. Don't trust the
dollar figures as a prediction of what the real API will actually charge.

**Cancellation reuses the real deterministic estimator** (``policy.compute_cancel_estimate``)
rather than reproducing it — same function the agent's own pre-confirmation estimate
uses, so the "confirmed" numbers are consistent with it by construction, the way the
real API is observed to be (DECISIONS.md §3, "Penalty formula, verified").

**Error injection**: add ``"_inject_errors": {"extend": "PAYMENT_DECLINED"}`` (or
``"quote"``, ``"cancel"``, ``"lookup"``) to a reservation JSON and the next call to that
operation raises the named ``AvisAPIError`` once, then clears itself — hand-edit the
file, rerun ``dev/run_local.py``, see how the agent handles it. Any code from
docs/api-reference.md's error lists works; unrecognized codes are treated as a generic
4xx (retryable=False) unless the name starts with ``TRANSIENT_`` (mapped to a 503,
retryable=True) or is literally ``TIMEOUT``/``CONNECTION_ERROR``.
"""
from __future__ import annotations

import copy
import math
from datetime import datetime, timezone
from typing import Any

import config
import policy
from avis_client import AvisAPIError

TAX_RATE = 0.0925
ONE_WAY_FEE_USD = 75.00  # placeholder — no captured example changes location


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt.replace("Z", "+00:00"))


class FakeBackend:
    """Mutable in-memory state for exactly one reservation, seeded from a JSON file.

    A confirmed extend updates the held state's current_return_datetime, so a
    subsequent lookup or a second extend in the same interactive session sees the new
    value — matching what a real backend would do, and what makes multi-turn testing
    (extend once, then check the new return date, then extend again) meaningful.
    """

    def __init__(self, reservation: dict):
        self.state = copy.deepcopy(reservation)
        self.reservation_id = self.state["reservation_id"]
        self._pending_errors: dict[str, str] = dict(self.state.pop("_inject_errors", {}) or {})

    def _maybe_raise(self, op: str) -> None:
        code = self._pending_errors.pop(op, None)
        if not code:
            return
        if code in {"TIMEOUT", "CONNECTION_ERROR"}:
            raise AvisAPIError(code, f"injected {code} for {op}", retryable=True)
        if code.startswith("TRANSIENT_") or code.startswith("HTTP_5"):
            raise AvisAPIError(code, f"injected transient failure for {op}",
                                status=503, retryable=True)
        status = {"VERIFICATION_FAILED": 403, "RESERVATION_NOT_FOUND": 404,
                  "RESERVATION_NOT_ACTIVE": 409, "VEHICLE_UNAVAILABLE": 409,
                  "PAYMENT_DECLINED": 402}.get(code, 400)
        raise AvisAPIError(code, f"injected {code} for {op}", status=status, retryable=False)

    # --- reads ------------------------------------------------------------------

    def get_reservation(self, reservation_id: str) -> dict:
        self._maybe_raise("lookup")
        if reservation_id != self.reservation_id:
            raise AvisAPIError("RESERVATION_NOT_FOUND",
                                f"No reservation {reservation_id!r} in this local sandbox "
                                f"(loaded: {self.reservation_id})", status=404)
        return copy.deepcopy(self.state)

    # --- pricing (shared by quote and the write endpoints) -----------------------

    def _price(self, new_return_datetime: str, new_return_location: str | None) -> dict:
        current = _parse(self.state["dates"]["current_return_datetime"])
        target = _parse(new_return_datetime)
        daily_rate = float(self.state["pricing"]["daily_rate"])
        extension_days = max(1, math.ceil((target - current).total_seconds() / 86400))
        subtotal = round(daily_rate * extension_days, 2)

        is_preferred = self.state["membership_status"] == "avis_preferred"
        overdue = current < datetime.now(timezone.utc).astimezone(current.tzinfo)
        late_fee = 0.0 if is_preferred or not overdue else config.LATE_RETURN_FEE_USD

        changing_location = bool(new_return_location and
                                  new_return_location != self.state["return_location"]["code"])
        one_way_fee = ONE_WAY_FEE_USD if changing_location else 0.0

        taxable = subtotal + late_fee + one_way_fee
        taxes_and_fees = round(taxable * TAX_RATE, 2)
        total = round(taxable + taxes_and_fees, 2)
        return {"daily_rate": daily_rate, "extension_days": extension_days,
                "subtotal": subtotal, "late_fee": late_fee, "one_way_fee": one_way_fee,
                "taxes_and_fees": taxes_and_fees, "total_charged": total, "currency": "USD"}

    def quote_change(self, reservation_id: str, change_type: str,
                      new_return_datetime: str, new_return_location: str | None = None) -> dict:
        self._maybe_raise("quote")
        charges = self._price(new_return_datetime, new_return_location)
        return {"success": True, "reservation_id": reservation_id, "change_type": change_type,
                "quote": {"current_return_datetime": self.state["dates"]["current_return_datetime"],
                          "new_return_datetime": new_return_datetime, "charges": charges}}

    # --- writes -------------------------------------------------------------------

    def _verify(self, email: str) -> None:
        # No email is captured in these synthetic payloads (the real API never returns
        # one either — BRIEF.md's Test accounts table is the only place it appears).
        # Any non-empty email verifies here; edit _inject_errors to test the failure
        # path deliberately instead.
        if not (email or "").strip():
            raise AvisAPIError("VERIFICATION_FAILED", "no email supplied", status=403)

    def extend_reservation(self, reservation_id: str, new_return_datetime: str,
                            email: str, cvv: str, billing_zip: str) -> dict:
        self._maybe_raise("extend")
        self._verify(email)
        if (self.state.get("status") or "").lower() != "active":
            raise AvisAPIError("RESERVATION_NOT_ACTIVE",
                                f"reservation status is {self.state.get('status')!r}", status=409)
        charges = self._price(new_return_datetime, None)
        self.state["dates"]["current_return_datetime"] = new_return_datetime
        self.state["payment"]["total_charged"] = round(
            self.state["payment"]["total_charged"] + charges["total_charged"], 2)
        return {"success": True, "confirmation_number": f"EXT-LOCAL-{self.reservation_id[-4:]}",
                "extension_details": {"extension_days": charges["extension_days"],
                                       "late_return": charges["late_fee"] > 0},
                "charges": charges}

    def cancel_reservation(self, reservation_id: str, email: str,
                            reason: str | None = None) -> dict:
        self._maybe_raise("cancel")
        self._verify(email)
        if (self.state.get("status") or "").lower() != "active":
            raise AvisAPIError("RESERVATION_NOT_ACTIVE",
                                f"reservation status is {self.state.get('status')!r}", status=409)
        estimate = policy.compute_cancel_estimate(self.state)
        self.state["status"] = "cancelled"
        return {"success": True, "confirmation_number": f"CXL-LOCAL-{self.reservation_id[-4:]}",
                "cancellation_details": {"penalty": estimate.penalty, "refund_amount": estimate.refund,
                                          "prepaid_amount": estimate.prepaid, "currency": estimate.currency}}


def patch_all(backend: FakeBackend) -> list[tuple[Any, str, Any]]:
    """Rebind every client reference the agent actually calls — not just avis_client's.

    extend_flow and cancel_flow bare-import their client functions
    (``from avis_client import extend_reservation``), so patching the attribute on the
    avis_client module leaves their bound names untouched — the exact bug class
    DECISIONS.md §2 documents catching in the test suite ("agent.py had imported the
    function by bare name, so patching the module attribute it was defined on did
    nothing"). Returns (module, name, original) triples for ``unpatch``.
    """
    import avis_client
    import cancel_flow
    import extend_flow

    targets = [
        (avis_client, "get_reservation", backend.get_reservation),
        (extend_flow, "quote_change", backend.quote_change),
        (extend_flow, "extend_reservation", backend.extend_reservation),
        (cancel_flow, "cancel_reservation", backend.cancel_reservation),
    ]
    originals = [(mod, name, getattr(mod, name)) for mod, name, _ in targets]
    for mod, name, fn in targets:
        setattr(mod, name, fn)
    return originals


def unpatch(originals: list[tuple[Any, str, Any]]) -> None:
    for mod, name, original in originals:
        setattr(mod, name, original)
