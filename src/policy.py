"""Deterministic cancellation-policy estimator.

The API has no cancel quote (`/quote` accepts only extend/modify), so the pre-confirmation
figure the customer agrees to is computed HERE, from published policy, and labelled an
estimate. The knowledge-base article is attached as the citation — never as the source of
arithmetic the model performs (DECISIONS.md §2, what the KB is not allowed to do).

Rules encoded, and where they come from:
- kb_can_01 (official-policy): >48h before pickup → full refund of prepaid; within 48h →
  penalty equal to one day's rate. Prepaid non-refundable rates are an exception — and the
  reservation payload has no rate-plan field, so that exception is undetectable. This alone
  is why the figure is permanently an estimate.
- kb_can_02 (help-center): refund = prepaid − penalty; pay-at-counter (no prepayment) →
  nothing to refund, cancelling simply releases the reservation.
- Observed live across all six test reservations: for in-rental cancellations the API
  charges penalty == daily_rate exactly, matching the within-48h formula.

Pure computation — no API call, no LLM, nothing to time out. Unit-tested directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

CITATION = {"article_id": "kb_can_01", "title": "Cancelling a Reservation",
            "authority": "official-policy"}

# The three policy branches, keyed by relationship of *now* to the pickup time.
PRE_PICKUP_FREE = "pre_pickup_gt_48h"       # >48h out: no penalty, full refund
PRE_PICKUP_PENALTY = "pre_pickup_le_48h"    # ≤48h out: one day's rate
IN_RENTAL = "in_rental"                     # pickup already passed: customer has the car


@dataclass
class CancelEstimate:
    branch: str
    penalty: float
    refund: float
    prepaid: float
    currency: str
    citation: dict = field(default_factory=lambda: dict(CITATION))
    caveats: list[str] = field(default_factory=list)
    # Set when the customer physically holds the car: "cancel" may really mean "early
    # return", which is handled at the counter with NO fee (kb_can_04). The agent must
    # disambiguate before anything is staged for confirmation.
    requires_disambiguation: bool = False


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(str(dt).replace("Z", "+00:00"))


def compute_cancel_estimate(reservation: dict, now: datetime | None = None) -> CancelEstimate:
    """Branch on live reservation state — never on the customer's wording."""
    now = now or datetime.now(timezone.utc)
    pickup = _parse(reservation["dates"]["pickup_datetime"])
    daily_rate = float(reservation["pricing"]["daily_rate"])
    prepaid = float(reservation["payment"]["total_charged"])
    currency = reservation.get("pricing", {}).get("currency", "USD")

    caveats = ["Prepaid non-refundable rates are not eligible for a refund regardless of "
               "timing; the reservation record does not expose the rate plan, so this "
               "estimate assumes a refundable rate. The final amount is confirmed at "
               "cancellation."]

    if prepaid <= 0:
        # Pay-at-counter: kb_can_02 — nothing to refund; cancelling releases the booking.
        hours_out = (pickup - now).total_seconds() / 3600
        return CancelEstimate(
            branch=PRE_PICKUP_FREE if hours_out > 0 else IN_RENTAL,
            penalty=0.0, refund=0.0, prepaid=0.0, currency=currency,
            caveats=["No prepayment on this reservation, so there is nothing to refund; "
                     "cancelling releases the booking. A no-show fee may apply at some "
                     "locations if it is not cancelled (kb_can_03)."],
            requires_disambiguation=(pickup - now).total_seconds() <= 0,
        )

    hours_until_pickup = (pickup - now).total_seconds() / 3600

    if hours_until_pickup > 48:
        return CancelEstimate(branch=PRE_PICKUP_FREE, penalty=0.0, refund=prepaid,
                              prepaid=prepaid, currency=currency, caveats=caveats)

    if hours_until_pickup > 0:
        penalty = round(min(daily_rate, prepaid), 2)
        return CancelEstimate(branch=PRE_PICKUP_PENALTY, penalty=penalty,
                              refund=round(prepaid - penalty, 2), prepaid=prepaid,
                              currency=currency, caveats=caveats)

    # Pickup has passed: the customer holds the vehicle. Cancelling is possible (observed
    # live) and charges one day's rate — but "cancel" said mid-rental very often means
    # "I'm done with the car", which is an EARLY RETURN: handled at the counter, no fee,
    # charges follow actual rental time (kb_can_04). Never assume; disambiguate.
    penalty = round(min(daily_rate, prepaid), 2)
    return CancelEstimate(
        branch=IN_RENTAL, penalty=penalty, refund=round(prepaid - penalty, 2),
        prepaid=prepaid, currency=currency,
        caveats=caveats + [
            "The vehicle is still in the customer's possession: cancelling does not end "
            "the physical rental, and the car must still be returned."],
        requires_disambiguation=True,
    )
