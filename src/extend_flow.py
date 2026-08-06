"""Extend workflow — quote, optional offers, and the gated write.

Kept separate from the agent definition so the money-touching logic is plain Python that
can be read and tested without an LLM in the loop. The agent layer only calls in here.
"""
from __future__ import annotations

import concurrent.futures
import time
from datetime import datetime, timedelta
from typing import Any

import config
from avis_client import AvisAPIError, extend_reservation, quote_change
from session import PendingQuote, ServicingSession, log_event


class FlowError(Exception):
    """A problem the agent should explain to the customer rather than retry blindly."""

    def __init__(self, message: str, *, escalate: bool = False, code: str | None = None):
        super().__init__(message)
        self.escalate = escalate
        self.code = code


def normalize_datetime(raw: str, session: ServicingSession) -> str:
    """Coerce a datetime into the pickup branch's local offset.

    Rental times are local to the branch (DECISIONS.md §5, timezones). A bare date or an
    offset-less datetime is interpreted in *branch* local time, never UTC, because
    "Friday 5pm" means 5pm at the counter.
    """
    text = (raw or "").strip()
    if not text:
        raise FlowError("No return date/time was provided.")

    offset = session.return_utc_offset
    current = (session.reservation or {}).get("dates", {}).get("current_return_datetime", "")

    if len(text) == 10:
        # Date only — keep the existing return time of day.
        time_of_day = current[11:19] if len(current) >= 19 else "12:00:00"
        normalized = f"{text}T{time_of_day}{offset}"
    else:
        has_offset = len(text) >= 6 and (text[-6] in "+-" or text.endswith("Z"))
        if not has_offset:
            if len(text) == 16:  # YYYY-MM-DDTHH:MM
                text += ":00"
            normalized = f"{text}{offset}"
        else:
            normalized = text

    # Customers say "June 17th"; the model may pass that straight through. Fail as a
    # FlowError the agent can recover from by re-asking, never as a raw ValueError that
    # escapes the tool.
    try:
        _parse(normalized)
    except ValueError:
        raise FlowError(
            f"Could not read {raw!r} as a date and time. Ask the customer to confirm the "
            "return date, then pass it as YYYY-MM-DD or YYYY-MM-DDTHH:MM."
        ) from None
    return normalized


def _parse(dt: str) -> datetime:
    return datetime.fromisoformat(dt.replace("Z", "+00:00"))


def _charges(quote_response: dict) -> dict:
    return quote_response.get("quote", {}).get("charges", {})


def build_quote(session: ServicingSession, new_return_datetime: str) -> dict:
    """Price an extension and stage it as the pending quote awaiting confirmation.

    Returns a structured summary for the agent to present. Also computes the optional
    offers, which are shadow-logged regardless of whether they are shown.
    """
    if not session.reservation:
        raise FlowError("Look up the reservation before quoting.")

    target = normalize_datetime(new_return_datetime, session)
    current = session.reservation["dates"]["current_return_datetime"]

    if _parse(target) <= _parse(current):
        raise FlowError(
            f"The requested return time ({target}) is not after the current return time "
            f"({current}). An extension must move the return later."
        )

    response = quote_change(session.reservation_id, "extend", target)
    charges = _charges(response)

    # A quote with no total is not a quote. Staging 0.0 and handing the model None would
    # leave the customer able to confirm an amount nobody ever stated, and the write
    # endpoint would then charge whatever it likes.
    total = charges.get("total_charged")
    if total is None:
        log_event("quote_incomplete", reservation_id=session.reservation_id,
                  new_return_datetime=target, charges=charges)
        raise FlowError(
            "The pricing service returned a quote with no total, so there is nothing to "
            "confirm. Tell the customer you couldn't price it and offer a representative."
        )

    session.pending_quote = PendingQuote(
        change_type="extend",
        new_return_datetime=target,
        total_charged=total,
        currency=charges.get("currency", "USD"),
        charges=charges,
        quoted_on_turn=session.turn,
    )
    log_event("quote_created", reservation_id=session.reservation_id,
              new_return_datetime=target, total=charges.get("total_charged"),
              charges=charges)

    summary: dict[str, Any] = {
        "current_return_datetime": current,
        "new_return_datetime": target,
        "charges": charges,
        "total_charged": charges.get("total_charged"),
        "currency": charges.get("currency", "USD"),
    }

    upgrade = _upgrade_offer(session, charges)
    if upgrade:
        summary["upgrade_offer"] = upgrade

    alternatives = _date_alternatives(session, target, charges)
    if alternatives:
        summary["cheaper_alternatives"] = alternatives

    return summary


def _upgrade_offer(session: ServicingSession, charges: dict) -> dict | None:
    """Membership-upgrade upsell, surfaced only where it genuinely offsets cost.

    Preferred members are exempt from the late return fee, so this only has something
    honest to say when a standard member is actually being charged one. Shadow-logged
    either way (it costs no API calls to compute).
    """
    late_fee = float(charges.get("late_fee") or 0.0)
    if session.is_preferred or late_fee <= 0:
        return None

    offer = {
        "membership_status": "standard",
        "late_fee_on_this_quote": late_fee,
        "benefit": "Avis Preferred members are exempt from late return fees.",
        "handling": "escalate_to_human_to_finalize",
    }
    log_event("upgrade_offer_computed", reservation_id=session.reservation_id,
              surfaced=config.IN_FLOW_UPGRADE_OFFER, **offer)
    return offer if config.IN_FLOW_UPGRADE_OFFER else None


def _date_alternatives(session: ServicingSession, target: str, charges: dict) -> list[dict] | None:
    """Quote nearby return dates and keep only materially cheaper ones.

    Never blocks the primary path: the speculative quotes fan out concurrently and any
    that fail or run slow are dropped silently.
    """
    mode = config.FLEXIBLE_DATE_ALTERNATIVES_MODE
    if mode not in {"shadow", "on"}:
        return None

    primary_total = float(charges.get("total_charged") or 0.0)
    base = _parse(target)
    candidates = [base - timedelta(days=1), base + timedelta(days=1)]
    candidates = [c for c in candidates
                  if c > _parse(session.reservation["dates"]["current_return_datetime"])]

    def price(candidate: datetime) -> dict | None:
        try:
            resp = quote_change(session.reservation_id, "extend", candidate.isoformat())
            return {"new_return_datetime": candidate.isoformat(),
                    "total_charged": _charges(resp).get("total_charged")}
        except Exception as exc:  # noqa: BLE001 - speculative work must never block
            # Dropped silently from the customer's view, but not from ours: an unlogged
            # blanket catch would hide a coding bug in here forever.
            log_event("alternative_quote_dropped", reservation_id=session.reservation_id,
                      candidate=candidate.isoformat(), error=f"{type(exc).__name__}: {exc}")
            return None

    # pool.map() waits for every candidate, and each one can burn the client's full retry
    # budget — roughly a minute worst case, blocking the reply the customer is waiting on.
    # Explicit per-future deadline keeps the "never blocks the primary path" promise real.
    # `with ThreadPoolExecutor(...)` shuts down with wait=True, which blocks on every
    # in-flight thread and silently defeats a per-future timeout. Shut down without
    # waiting instead: a straggler finishes into the void (its own request timeout bounds
    # it) while the customer's answer goes out on schedule.
    results = []
    deadline = time.monotonic() + config.ALTERNATIVE_QUOTE_BUDGET_S
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(candidates) or 1)
    try:
        futures = {pool.submit(price, c): c for c in candidates}
        for future, candidate in futures.items():
            try:
                outcome = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError:
                log_event("alternative_quote_dropped", reservation_id=session.reservation_id,
                          candidate=candidate.isoformat(), error="budget exceeded")
                continue
            if outcome and outcome["total_charged"] is not None:
                results.append(outcome)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    threshold = max(config.ALTERNATIVE_MATERIALITY_ABS_USD,
                    primary_total * config.ALTERNATIVE_MATERIALITY_PCT)
    material = [r for r in results if primary_total - float(r["total_charged"]) >= threshold]
    material.sort(key=lambda r: r["total_charged"])
    material = material[:config.MAX_ALTERNATIVES]

    # would_surface is the counterfactual Avis measures in shadow mode: what the customer
    # would have been shown, and the saving they would have been offered.
    log_event("date_alternatives_computed", reservation_id=session.reservation_id,
              mode=mode, primary_total=primary_total, threshold=round(threshold, 2),
              priced=results, would_surface=material,
              max_saving=round(primary_total - float(material[0]["total_charged"]), 2) if material else 0.0,
              surfaced=bool(material) and mode == "on")
    return material if (mode == "on" and material) else None


def revalidate_quote(session: ServicingSession) -> dict | None:
    """Re-price a stale quote before writing; report any delta instead of charging it.

    The confirmation gate is only meaningful if the price the customer agreed to is the
    price they get. If the quote has aged past its TTL we re-quote, and if the total
    moved we refuse the write and hand the new number back for re-confirmation.
    """
    quote = session.pending_quote
    if not quote or quote.age_seconds() < config.QUOTE_TTL_SECONDS:
        return None

    response = quote_change(session.reservation_id, "extend", quote.new_return_datetime)
    charges = _charges(response)
    new_total = charges.get("total_charged", quote.total_charged)
    old_total = quote.total_charged

    quote.charges = charges
    quote.total_charged = new_total
    quote.quoted_at = time.monotonic()

    if abs(float(new_total) - float(old_total)) >= 0.01:
        log_event("quote_reprice_delta", reservation_id=session.reservation_id,
                  old_total=old_total, new_total=new_total)
        return {"repriced": True, "previous_total": old_total, "new_total": new_total,
                "currency": quote.currency, "charges": charges}
    log_event("quote_revalidated", reservation_id=session.reservation_id, total=new_total)
    return None


def commit_extension(session: ServicingSession, email: str, cvv: str, billing_zip: str) -> dict:
    """The gated write. Returns a structured outcome; never raises on API errors.

    This is the confirmation gate in code rather than in a prompt: without a quote the
    customer was shown staged in session state, there is no path to a charge. Outcome
    keys the agent branches on: ``ok``, ``escalate``, ``price_changed``.
    """
    if not session.reservation:
        return {"ok": False, "customer_message": "Look up the reservation first."}
    if not session.pending_quote:
        return {"ok": False, "customer_message":
                "No quote has been given yet. Quote the extension and confirm the total "
                "with the customer before charging."}
    if session.pending_quote.consumed:
        return {"ok": False, "already_processed": True, "customer_message":
                "This extension has already been processed. Give the customer the "
                "confirmation number you already have — do not charge again."}
    if session.turn <= session.pending_quote.quoted_on_turn:
        return {"ok": False, "customer_message":
                "The customer has not seen this quote yet. State the total, wait for them "
                "to agree, and only then charge."}

    try:
        delta = revalidate_quote(session)
    except AvisAPIError as exc:
        return classify_failure(session, exc, "revalidate_quote")
    if delta:
        return {"ok": False, "price_changed": True, "customer_message":
                f"The price changed from {delta['previous_total']} to {delta['new_total']} "
                f"{delta['currency']}. Show the new total and get agreement again before charging.",
                **delta}

    quote = session.pending_quote
    try:
        result = extend_reservation(session.reservation_id, quote.new_return_datetime,
                                    email.strip(), cvv.strip(), billing_zip.strip())
    except AvisAPIError as exc:
        if exc.code == "VERIFICATION_FAILED":
            session.failed_verifications += 1
            log_event("verification_failed", reservation_id=session.reservation_id,
                      attempt=session.failed_verifications)
            if session.failed_verifications >= config.MAX_VERIFICATION_ATTEMPTS:
                session.escalated = True
                # Terminal: without this the lockout is announced but the caller can keep
                # guessing emails and charging. Same bug class as the 2026-08-05 incident,
                # reached through a different code path.
                session.handed_off = True
                session.escalation_reason = "repeated verification failure"
                return {"ok": False, "escalate": True, "code": exc.code,
                        "customer_message": "We could not verify the account after several attempts.",
                        "handoff": session.escalation_payload()}
            return {"ok": False, "code": exc.code, "customer_message":
                    "That email doesn't match our records for this reservation. "
                    f"Attempt {session.failed_verifications} of {config.MAX_VERIFICATION_ATTEMPTS}."}
        if exc.code == "PAYMENT_VALIDATION_ERROR":
            return {"ok": False, "code": exc.code, "customer_message":
                    "The CVV or billing ZIP wasn't accepted. Ask the customer to re-check both."}
        return classify_failure(session, exc, "commit_extension")

    session.verified_email = email.strip()
    quote.accepted = True
    quote.consumed = True
    session.note("completed_extension", {
        "confirmation_number": result.get("confirmation_number"),
        "new_return_datetime": quote.new_return_datetime,
    })
    log_event("extension_confirmed", reservation_id=session.reservation_id,
              confirmation_number=result.get("confirmation_number"), total=quote.total_charged)
    return {"ok": True, "confirmation_number": result.get("confirmation_number"),
            "extension_details": result.get("extension_details"),
            "charges": result.get("charges")}


# Error codes that mean "a human should take this", not "try again".
ESCALATING_CODES = {"RESERVATION_NOT_ACTIVE", "PAYMENT_DECLINED", "EXHAUSTED_RETRIES",
                    "INVALID_EXTENSION", "MALFORMED_RESERVATION"}


def classify_failure(session: ServicingSession, exc: AvisAPIError, operation: str) -> dict:
    """Map an API error to a conversational outcome, escalating where the line sits.

    Bias toward escalation (DECISIONS.md §3): wrongly escalating costs a few dollars of
    handling time; wrongly charging a customer costs revenue and trust.
    """
    log_event("api_failure", operation=operation, code=exc.code, status=exc.status,
              retryable=exc.retryable, reservation_id=session.reservation_id)
    if exc.code in ESCALATING_CODES:
        session.escalated = True
        # A classified terminal failure IS a handoff. Reporting one while leaving every
        # action tool usable is exactly the contradiction customers noticed.
        session.handed_off = True
        session.escalation_reason = f"{operation} failed: {exc.code}"
        return {"ok": False, "escalate": True, "code": exc.code,
                "customer_message": str(exc), "handoff": session.escalation_payload()}
    return {"ok": False, "escalate": False, "code": exc.code,
            "customer_message": str(exc), "details": exc.details}
