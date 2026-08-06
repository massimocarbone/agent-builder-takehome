"""Cancel workflow — policy estimate, gated write, post-write variance check.

Mirrors extend_flow: money logic in plain Python, testable with no LLM. The structural
difference from Extend (DECISIONS.md §3): there is no cancel quote endpoint, so the
confirmation gate rests on a policy-derived ESTIMATE, and the only moment reality can be
compared against what the customer agreed to is AFTER the write. The variance check is
therefore remediation, not prevention — asymmetric, and biased toward escalation.
"""
from __future__ import annotations

from datetime import datetime, timezone

import config
import policy
from avis_client import AvisAPIError, cancel_reservation
from extend_flow import FlowError, classify_failure
from session import PendingCancellation, ServicingSession, log_event


def build_cancel_estimate(session: ServicingSession) -> dict:
    """Estimate the penalty/refund and stage it for confirmation.

    Branches on live reservation state (never the customer's verb). For an in-rental
    reservation the summary carries the early-return alternative — the agent must
    disambiguate 'cancel' before asking anyone to confirm anything, because cancelling
    costs one day's rate while returning early at the counter costs nothing (kb_can_04).
    """
    if not session.reservation:
        raise FlowError("Look up the reservation before estimating a cancellation.")
    if (session.reservation.get("status") or "").lower() != "active":
        raise FlowError(
            f"This reservation is not active (status: {session.reservation.get('status')}). "
            "There is nothing to cancel; explain and offer a representative if the customer "
            "disputes it.")

    estimate = policy.compute_cancel_estimate(session.reservation)

    session.pending_cancellation = PendingCancellation(
        branch=estimate.branch,
        penalty_estimate=estimate.penalty,
        refund_estimate=estimate.refund,
        prepaid=estimate.prepaid,
        currency=estimate.currency,
        citation=estimate.citation,
        caveats=estimate.caveats,
        quoted_on_turn=session.turn,
        requires_disambiguation=estimate.requires_disambiguation,
    )
    log_event("cancel_estimate_created", reservation_id=session.reservation_id,
              branch=estimate.branch, penalty=estimate.penalty, refund=estimate.refund,
              prepaid=estimate.prepaid, requires_disambiguation=estimate.requires_disambiguation)

    summary = {
        "branch": estimate.branch,
        "estimated_penalty": estimate.penalty,
        "estimated_refund": estimate.refund,
        "prepaid_amount": estimate.prepaid,
        "currency": estimate.currency,
        "citation": estimate.citation,
        "caveats": estimate.caveats,
        "is_estimate": True,
        "note": "These figures are a policy-based estimate; the final amount is confirmed "
                "at cancellation. Present them as an estimate, never as a quote.",
    }

    if estimate.requires_disambiguation:
        summary["requires_disambiguation"] = True
        summary["early_return_alternative"] = {
            "what": "The customer has the vehicle. If they mean 'I'm done with the car', "
                    "that is an EARLY RETURN, not a cancellation: no fee, charges follow "
                    "actual rental time, handled at the branch counter — no action here.",
            "citation": {"article_id": "kb_can_04", "title": "Ending a Rental Early",
                         "authority": "help-center"},
            "warning": "Cancelling instead would charge the penalty above AND the car "
                       "must still be returned. Ask which they mean before confirming.",
        }

    _retention_prompt(session, estimate)
    return summary


def _retention_prompt(session: ServicingSession, estimate: policy.CancelEstimate) -> None:
    """CANCEL_RETENTION_PROMPT: offer a date change once before cancelling.

    Costs nothing to compute, so the counterfactual is always logged. Surfacing rules
    when on (DECISIONS.md §3): offered ONCE, a 'no' is accepted immediately, and the
    cancellation is never gated on hearing the offer.
    """
    already_offered = session.collected_context.get("retention_prompt_offered", False)
    would_offer = not already_offered and estimate.branch != policy.IN_RENTAL
    log_event("retention_prompt_computed", reservation_id=session.reservation_id,
              surfaced=bool(config.CANCEL_RETENTION_PROMPT and would_offer),
              would_offer=would_offer, branch=estimate.branch)
    if config.CANCEL_RETENTION_PROMPT and would_offer:
        session.note("retention_prompt_offered", True)


TRUE_CANCELLATION = "true_cancellation"
EARLY_RETURN = "early_return"


def resolve_cancel_intent(session: ServicingSession, intent: str) -> dict:
    """Record which of the two mid-rental outcomes the customer actually chose.

    The disambiguation in ``build_cancel_estimate`` was advisory until this existed: the
    summary told the model to ask, and nothing checked that it had. A customer who says
    "I'm done with it, I'll bring it back" and gets cancelled anyway pays one day's rate
    for a word choice — the trust failure DECISIONS.md §3 names as invisible to anyone not
    looking for it. So the answer becomes state, and the write reads it.

    ``early_return`` discards the staged estimate outright: there is nothing to commit,
    and leaving it staged leaves it committable.
    """
    staged = session.pending_cancellation
    if not staged:
        return {"ok": False, "customer_message":
                "There's no cancellation estimate on the table. Estimate it first."}
    if intent not in {TRUE_CANCELLATION, EARLY_RETURN}:
        return {"ok": False, "customer_message":
                f"Intent must be {TRUE_CANCELLATION!r} or {EARLY_RETURN!r}. If the "
                "customer's answer wasn't clearly one of those, ask again — don't guess."}

    log_event("cancel_intent_resolved", reservation_id=session.reservation_id,
              intent=intent, branch=staged.branch, turn=session.turn)

    if intent == EARLY_RETURN:
        session.pending_cancellation = None
        session.note("resolved_as_early_return", True)
        return {"ok": True, "intent": EARLY_RETURN, "nothing_cancelled": True,
                "customer_message":
                    "Nothing has been cancelled and nothing will be charged for the "
                    "change. Tell the customer to return the vehicle to the branch; the "
                    "rental is billed for the time actually used, with no early-return "
                    "fee (kb_can_04). The reservation stays as it is.",
                "citation": {"article_id": "kb_can_04", "title": "Ending a Rental Early"}}

    staged.intent_confirmed = True
    return {"ok": True, "intent": TRUE_CANCELLATION,
            "customer_message":
                "Recorded that the customer wants a true cancellation. Restate the "
                "estimated penalty and refund and that the vehicle must still be "
                "returned, then take their explicit agreement before confirming."}


def commit_cancellation(session: ServicingSession, email: str,
                        reason: str | None = None) -> dict:
    """The gated write, then the variance check against what the customer agreed to.

    Same locks as commit_extension: staged estimate, single-use, turn boundary,
    verification counted toward lockout. Cancel needs no payment block — email only.
    """
    if not session.reservation:
        return {"ok": False, "customer_message": "Look up the reservation first."}
    staged = session.pending_cancellation
    if not staged:
        return {"ok": False, "customer_message":
                "No cancellation estimate has been given yet. Estimate it, show the "
                "customer the penalty and refund, and get explicit agreement first."}
    if staged.consumed:
        return {"ok": False, "already_processed": True, "customer_message":
                "This cancellation has already been processed. Give the customer the "
                "confirmation number you already have — do not cancel again."}
    if session.turn <= staged.quoted_on_turn:
        return {"ok": False, "customer_message":
                "The customer has not seen this estimate yet. State the penalty and "
                "refund, wait for them to agree, and only then cancel."}
    if staged.requires_disambiguation and not staged.intent_confirmed:
        return {"ok": False, "requires_disambiguation": True, "customer_message":
                "The customer is holding the vehicle, so 'cancel' is ambiguous and this "
                "is blocked until it's resolved. Ask whether they mean a true "
                "cancellation (penalty of one day's rate, refund of the remainder, and "
                "the car still has to come back) or an early return (no fee, handled at "
                "the counter, nothing processed here). Record their answer with "
                "resolve_cancel_intent, then come back."}

    try:
        result = cancel_reservation(session.reservation_id, email.strip(), reason=reason)
    except AvisAPIError as exc:
        if exc.code == "VERIFICATION_FAILED":
            session.failed_verifications += 1
            log_event("verification_failed", reservation_id=session.reservation_id,
                      attempt=session.failed_verifications, operation="cancel")
            if session.failed_verifications >= config.MAX_VERIFICATION_ATTEMPTS:
                session.escalated = True
                session.handed_off = True
                session.escalation_reason = "repeated verification failure on cancel"
                return {"ok": False, "escalate": True, "code": exc.code,
                        "customer_message": "We could not verify the account after several attempts.",
                        "handoff": session.escalation_payload()}
            return {"ok": False, "code": exc.code, "customer_message":
                    "That email doesn't match our records for this reservation."}
        return classify_failure(session, exc, "commit_cancellation")

    session.verified_email = email.strip()
    staged.consumed = True
    details = result.get("cancellation_details", {}) or {}
    actual_penalty = float(details.get("penalty") or 0.0)
    actual_refund = float(details.get("refund_amount") or 0.0)

    outcome = {
        "ok": True,
        "confirmation_number": result.get("confirmation_number"),
        "actual_penalty": actual_penalty,
        "actual_refund": actual_refund,
        "prepaid_amount": details.get("prepaid_amount"),
        "currency": details.get("currency", staged.currency),
        "refund_timing": "Refunds are issued to the original form of payment, typically "
                         "within 5 to 10 business days (kb_can_02).",
    }
    session.note("completed_cancellation", {
        "confirmation_number": outcome["confirmation_number"],
        "actual_penalty": actual_penalty, "actual_refund": actual_refund})
    log_event("cancellation_confirmed", reservation_id=session.reservation_id,
              confirmation_number=outcome["confirmation_number"],
              estimated_penalty=staged.penalty_estimate, actual_penalty=actual_penalty,
              estimated_refund=staged.refund_estimate, actual_refund=actual_refund)

    # Variance check — asymmetric by design. Adverse (customer pays more / receives less
    # than they agreed to) escalates: the charge has already happened, so this is
    # remediation, and a human owns making it right. Favorable is just good news.
    threshold = config.CANCEL_VARIANCE_THRESHOLD_USD
    penalty_delta = actual_penalty - staged.penalty_estimate
    refund_delta = staged.refund_estimate - actual_refund
    if penalty_delta >= threshold or refund_delta >= threshold:
        session.escalated = True
        session.handed_off = True
        session.escalation_reason = (
            f"cancel outcome adverse vs estimate: penalty {staged.penalty_estimate}->"
            f"{actual_penalty}, refund {staged.refund_estimate}->{actual_refund}")
        log_event("cancel_variance_adverse", reservation_id=session.reservation_id,
                  penalty_delta=round(penalty_delta, 2), refund_delta=round(refund_delta, 2))
        outcome.update({
            "escalate": True,
            "variance": {"estimated_penalty": staged.penalty_estimate,
                         "estimated_refund": staged.refund_estimate,
                         "actual_penalty": actual_penalty, "actual_refund": actual_refund},
            "handoff": session.escalation_payload(),
            "customer_message":
                "The final amount differs from the estimate the customer agreed to, in "
                "the customer's disfavor. Tell them plainly: the cancellation went "
                "through, state both numbers, and say a representative will review and "
                "make it right. Then stop working the request.",
        })
        return outcome

    if penalty_delta <= -threshold or refund_delta <= -threshold:
        outcome["better_than_estimate"] = True
        log_event("cancel_variance_favorable", reservation_id=session.reservation_id,
                  penalty_delta=round(penalty_delta, 2), refund_delta=round(refund_delta, 2))

    return outcome
