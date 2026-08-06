"""Avis servicing agent — Extend workflow.

Layer 2 of the architecture (DECISIONS.md §2). The model drives the conversation; it
does not decide when money moves. The confirmation gate is enforced in code: the write
tool is unreachable unless a quote the customer was shown is staged in session state and
the customer has actively supplied the card's CVV and billing ZIP.

Run it:
    python src/agent.py
"""
from __future__ import annotations

import os
import random
import sys
import time
import uuid

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agents import Agent, RunContextWrapper, Runner, SQLiteSession, function_tool  # noqa: E402

import cancel_flow  # noqa: E402
import config  # noqa: E402
import extend_flow  # noqa: E402
import kb  # noqa: E402
import avis_client  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from session import ServicingSession, log_event  # noqa: E402

# --- Tools --------------------------------------------------------------------------
# Thin wrappers. The money-touching logic lives in extend_flow so it can be read and
# tested without an LLM in the loop.

_HANDED_OFF = {
    "ok": False,
    "handed_off": True,
    "customer_message": (
        "This conversation has been handed to a representative. Do not look anything up, "
        "quote, or change the booking. Acknowledge the customer warmly and let them know "
        "the representative has everything and will pick it up."
    ),
}


def _blocked(session: ServicingSession) -> dict | None:
    """After a hard handoff, action tools refuse.

    Enforced here rather than asked for in the prompt: on 2026-08-05 the agent announced a
    handoff and then serviced the request itself 19 seconds later. A state the model can
    narrate but not be bound by is not a state.
    """
    return _HANDED_OFF if session.handed_off else None


@function_tool
def lookup_reservation(ctx: RunContextWrapper[ServicingSession], reservation_id: str) -> dict:
    """Look up an Avis reservation by ID (e.g. 'AVS-29471835').

    Call this whenever a customer references a reservation — for a change OR just to
    answer a question about it. Everything this returns may be shared with the caller
    except `card_last_four`, which needs verification first.
    """
    session = ctx.context
    if blocked := _blocked(session):
        return blocked
    try:
        reservation = avis_client.get_reservation(reservation_id.strip().upper())
    except AvisAPIError as exc:
        return extend_flow.classify_failure(session, exc, "lookup_reservation")

    session.load_reservation(reservation)
    log_event("reservation_loaded", reservation_id=reservation.get("reservation_id"),
              status=reservation.get("status"), membership=reservation.get("membership_status"))

    # The card's last four is withheld until the email has actually been checked against
    # the reservation — which only happens when a write succeeds. Supplying an email is
    # a claim, not proof, and reads are open to anyone holding a reservation id. Enforced
    # by omission: what the model never receives, it cannot disclose.
    dates = reservation.get("dates", {})
    return {
        "ok": True,
        "reservation_id": reservation.get("reservation_id"),
        "customer_name": reservation.get("customer_name"),
        "membership_status": reservation.get("membership_status"),
        "status": reservation.get("status"),
        "vehicle": reservation.get("vehicle", {}).get("description"),
        "pickup_location": reservation.get("pickup_location", {}).get("name"),
        "return_location": reservation.get("return_location", {}).get("name"),
        "pickup_datetime": dates.get("pickup_datetime"),
        "current_return_datetime": dates.get("current_return_datetime"),
        "daily_rate": reservation.get("pricing", {}).get("daily_rate"),
        "total_charged": reservation.get("payment", {}).get("total_charged"),
        "card_last_four": (
            reservation.get("payment", {}).get("card_on_file", {}).get("last_four")
            if session.verified_email else "withheld until verified"),
    }


@function_tool
def quote_extension(ctx: RunContextWrapper[ServicingSession], new_return_datetime: str) -> dict:
    """Price an extension to a new return date/time, before anything is charged.

    Always call this and show the customer the total before asking them to confirm.
    Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'; times are interpreted in the rental
    branch's local timezone.
    """
    session = ctx.context
    if blocked := _blocked(session):
        return blocked
    try:
        return {"ok": True, **extend_flow.build_quote(session, new_return_datetime)}
    except extend_flow.FlowError as exc:
        return {"ok": False, "customer_message": str(exc)}
    except AvisAPIError as exc:
        return extend_flow.classify_failure(session, exc, "quote_extension")


@function_tool
def confirm_extension(ctx: RunContextWrapper[ServicingSession], email: str, cvv: str,
                      billing_zip: str) -> dict:
    """Charge and commit the extension the customer just agreed to.

    Only call this after quote_extension has run, the customer has seen the total, and
    they have explicitly agreed and provided the card's CVV and billing ZIP. If the
    quote has gone stale this re-prices first and returns the new total for
    re-confirmation instead of charging a price the customer never accepted.
    """
    if blocked := _blocked(ctx.context):
        return blocked
    return extend_flow.commit_extension(ctx.context, email, cvv, billing_zip)


@function_tool
def estimate_cancellation(ctx: RunContextWrapper[ServicingSession]) -> dict:
    """Estimate the penalty and refund for cancelling the loaded reservation.

    Call this when a customer wants to cancel, BEFORE asking them to confirm anything.
    Returns policy-based ESTIMATES — always present them as estimates, with the caveat
    that the final amount is confirmed at cancellation. If the result includes
    requires_disambiguation, the customer has the car: ask whether they mean a true
    cancellation (penalty applies, car still must be returned) or an early return
    (no fee, handled at the counter, nothing to process here) before going further.
    """
    session = ctx.context
    if blocked := _blocked(session):
        return blocked
    try:
        return {"ok": True, **cancel_flow.build_cancel_estimate(session)}
    except extend_flow.FlowError as exc:
        return {"ok": False, "customer_message": str(exc)}
    except AvisAPIError as exc:
        return extend_flow.classify_failure(session, exc, "estimate_cancellation")


@function_tool
def confirm_cancellation(ctx: RunContextWrapper[ServicingSession], email: str,
                         reason: str = "") -> dict:
    """Cancel the reservation the customer just agreed to cancel.

    Only call after estimate_cancellation has run, the customer has seen the estimated
    penalty and refund, explicitly agreed, and given the email on file. Pass their stated
    reason if they offered one. The result contains the ACTUAL penalty and refund — if it
    flags escalate, the outcome differed from the estimate in the customer's disfavor:
    state both numbers plainly and hand off.
    """
    if blocked := _blocked(ctx.context):
        return blocked
    return cancel_flow.commit_cancellation(ctx.context, email, reason=reason or None)


@function_tool
def search_policy(query: str) -> dict:
    """Search Avis policy articles for questions about rules, fees, grace periods,
    refund timing, membership benefits, and similar. Returns the most relevant articles
    with their authority level and last-updated date.

    Use this for any policy question instead of answering from memory. If it returns no
    articles, say you don't have policy on that and offer a representative — do not
    guess. Never use an article to compute a price; prices come from quote tools only.

    Deliberately NOT blocked after a handoff: a customer waiting on a representative may
    still ask "how long do refunds take?", and answering general published policy neither
    touches their booking nor reveals anything about it. Everything that reads or changes
    the reservation stays blocked.
    """
    results = kb.search(query)
    if not results:
        return {"ok": True, "articles": [],
                "note": "No policy found. Say so and offer a representative; do not improvise."}
    return {"ok": True, "articles": results}


@function_tool
def escalate_to_human(ctx: RunContextWrapper[ServicingSession], reason: str,
                      customer_intent: str, reservation_id: str = "",
                      kind: str = "hard") -> dict:
    """Hand off to a human agent, passing along everything collected so far.

    kind="hard" — a real transfer. Use for actions this agent must not take (cancel,
    membership upgrade, location change), repeated verification failure, or an error that
    re-collecting input won't fix. After this you stop working the request entirely.

    kind="assistive" — you're pulling a person in to help with something you can't do,
    but the customer may still resolve it themselves (e.g. they can't find their
    reservation ID). The conversation stays open; if they find it, carry on normally.

    Pass reservation_id if the customer has given one, even if you haven't looked it up.
    The customer should never have to repeat information they already gave.
    """
    session = ctx.context

    # A handoff is only worth anything if it carries context. If the customer gave a
    # reservation id but the flow escalated before it was ever looked up, load it here —
    # otherwise the human receives an empty envelope and re-interviews the customer,
    # which is the exact friction this handoff exists to prevent.
    if not session.reservation and reservation_id.strip():
        try:
            session.load_reservation(avis_client.get_reservation(reservation_id.strip().upper()))
        except AvisAPIError as exc:
            log_event("escalation_lookup_failed", reservation_id=reservation_id,
                      code=exc.code)

    hard = kind != "assistive"
    session.escalated = True
    session.escalation_reason = reason
    session.handed_off = hard
    session.note("customer_intent", customer_intent)
    payload = session.escalation_payload()
    log_event("escalated", kind="hard" if hard else "assistive", reason=reason,
              reservation_id=session.reservation_id, payload=payload)

    if hard:
        return {"ok": True, "escalated": True, "handed_off": True, "handoff": payload,
                "customer_message":
                    "I'm connecting you with a representative and passing along everything "
                    "we've covered — including our conversation — so they can pick up where "
                    "we left off. Tell the customer this, then stop working the request."}
    return {"ok": True, "escalated": True, "handed_off": False, "handoff": payload,
            "customer_message":
                "A representative can help with this. Stay with the customer meanwhile — if "
                "they resolve what was blocking them, carry on as normal."}


# --- Agent --------------------------------------------------------------------------

INSTRUCTIONS = """\
You are an automated Avis rental servicing assistant. Say so if anyone asks whether
they're talking to a person, and don't pretend otherwise.

You help customers with an existing rental. You can:
- look up a reservation and answer questions about it
- answer policy questions from the Avis knowledge base
- EXTEND a rental — push out the return date/time
- CANCEL a reservation — with the customer's informed, explicit agreement

WHAT YOU MAY TELL A CALLER
Once you have looked a reservation up, you may freely share: the vehicle, the pickup and
return dates/times, pickup and return locations, the daily rate, the amount already
charged, membership status, and the reservation status. Answering "when is my car due
back?" is a normal, supported thing to do — the customer does not have to be making a
change to get an answer about their own booking, and you never require them to start an
extension in order to be told something.

You may NOT read out the card's last four digits until the customer is verified. A caller
merely telling you an address is not verification — verification happens when a change
goes through against the email on file. Until then the lookup returns "withheld until
verified" for the card, and you refer to it only as "the card on file". That is the only
restriction on reservation details.

EXTENDING, in order:
1. Get the reservation ID and look it up. Confirm you have the right rental by naming the
   vehicle and current return time.
2. Find out the new return date/time they want. Times are local to the rental branch.
3. Quote it. Always state the total clearly, in currency, and say what it covers before
   asking for agreement.
4. Get explicit agreement to that total. Then ask for the email on file plus the card's
   CVV and billing ZIP — all three are required to authorize the charge.
5. Confirm the extension and give them the confirmation number.

CANCELLING, in order:
1. Look up the reservation, then call estimate_cancellation.
2. If it says requires_disambiguation, the customer HAS the car. "Cancel" mid-rental
   usually means "I'm done with it" — which is an early return: no fee, charges follow
   the time actually rented, and it happens at the branch counter, not here. Explain both
   options with their costs and ask which they mean. If they mean early return, tell them
   to return the car and do NOT cancel anything. Only proceed if they clearly want a true
   cancellation, knowing the penalty AND that the car must still be returned.
3. Present the figures as an ESTIMATE — say the exact word "estimate" and that the final
   amount is confirmed at cancellation. Never call it a quote.
4. Get explicit agreement to those figures, then ask for the email on file (cancellation
   needs no card details).
5. Confirm the cancellation. Give the confirmation number, the ACTUAL penalty and refund,
   and the refund timing. If the result says escalate, the outcome was worse than the
   estimate: state both numbers plainly, apologize once, say a representative will review
   and make it right, and stop.
Cancellation is irreversible — never call confirm_cancellation on an ambiguous request,
and never rush a hesitating customer. If they change their mind at any point, drop it
immediately and confirm nothing was cancelled.

RULES YOU DO NOT BEND
- Never charge anything the customer has not seen and agreed to. If a tool says the price
  changed, show the new total and get agreement again.
- Never invent prices, fees, policies, rules, or confirmation numbers. Every number you
  state must come from a tool result.
- Never invent a REASON either. If you're asked why a fee applies and no retrieved article
  explains it, say plainly that it's the standard fee and you don't have detail on why it
  landed on this booking. Do not reason your way to a policy that sounds plausible.
- Never describe your own limits as "Avis policy" or "for security reasons" unless a
  retrieved article actually says so. If you can't do something, it's a limit of what this
  service handles — say that, in those words.
- For policy questions (grace periods, fees, refund timing, membership benefits) use
  search_policy and answer from what it returns. If it returns nothing, say you don't have
  that policy on hand and offer a representative. Articles explain rules; they are never a
  source for the price of this customer's change — only quotes are.

WHAT YOU HAND OFF
- Actions you cannot take: changing the return location or vehicle class, and Avis
  Preferred membership upgrades. Say so directly — don't imply you might manage it — and
  escalate with kind="hard".
- If a customer wants a membership upgrade, you never process it. Escalate so a
  representative can finalize the upgrade and any pending extension together.
- Use kind="assistive" when you're bringing in help but the customer might still resolve
  it themselves (e.g. they can't find their reservation ID). Stay with them; if they find
  it, carry on.
- Whenever you escalate, pass the reservation ID if you have one and summarize what the
  customer actually wants. They should never repeat themselves to the representative.
- After a hard handoff you stop working the request. Don't look things up, quote, or
  change anything — the tools will refuse anyway. Be warm, confirm the representative has
  everything, and wait with them.
- If the customer is going in circles, getting frustrated, or you've failed twice to help,
  offer a representative rather than repeating yourself.

Be warm and brief. This is a phone-style conversation, not a form.
"""

servicing_agent = Agent[ServicingSession](
    name="Avis Servicing Agent",
    model=config.AGENT_MODEL,
    instructions=INSTRUCTIONS,
    tools=[lookup_reservation, quote_extension, confirm_extension,
           estimate_cancellation, confirm_cancellation, search_policy,
           escalate_to_human],
)


def run_turn(user_input: str, servicing_session: ServicingSession, history: SQLiteSession,
             max_attempts: int = 5):
    """Run one conversational turn, absorbing LLM-provider rate limits.

    The Avis API is not the only imperfect dependency — the model provider throttles too,
    and a 429 mid-conversation would otherwise drop a customer who has already handed over
    their card details. Same principle as the API client: back off and retry the transient
    thing, surface the terminal thing.
    """
    servicing_session.turn += 1
    servicing_session.record("customer", user_input)
    for attempt in range(1, max_attempts + 1):
        try:
            result = Runner.run_sync(servicing_agent, user_input,
                                     context=servicing_session, session=history)
            servicing_session.record("agent", str(result.final_output))
            return result
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise varied types
            transient = type(exc).__name__ in {"RateLimitError", "APIConnectionError",
                                               "APITimeoutError", "InternalServerError"}
            log_event("llm_call_failed", type=type(exc).__name__, attempt=attempt,
                      transient=transient, error=str(exc)[:200])
            if not transient or attempt == max_attempts:
                raise
            time.sleep(min(60, 20 * attempt) + random.uniform(0, 3))
    raise RuntimeError("unreachable")


def main() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY in your .env first (see env.example).")
        return

    # A fresh conversation id per run. SQLiteSession.clear_session() is async, so calling
    # it synchronously silently does nothing and the previous customer's transcript would
    # carry into this one — a privacy bug, not just stale context.
    servicing_session = ServicingSession()
    history = SQLiteSession(f"avis-cli-{uuid.uuid4()}")

    print("Avis servicing agent — extend or cancel an existing rental. Ctrl-C or 'quit' to exit.\n")
    print("Agent: Hi! I'm Avis's automated assistant — I can help extend or cancel a "
          "reservation, or answer questions about one. Do you have your reservation number handy?")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return
        if user_input.lower() in {"quit", "exit"}:
            return
        if not user_input:
            continue

        try:
            result = run_turn(user_input, servicing_session, history)
            print(f"\nAgent: {result.final_output}")
        except Exception as exc:  # noqa: BLE001 - CLI must not die mid-conversation
            log_event("agent_run_error", error=str(exc), type=type(exc).__name__)
            print(f"\nAgent: Sorry — something went wrong on my end ({type(exc).__name__}). "
                  "Let me connect you with a representative.")


if __name__ == "__main__":
    main()
