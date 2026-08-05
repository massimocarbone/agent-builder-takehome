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

import config  # noqa: E402
import extend_flow  # noqa: E402
import kb  # noqa: E402
from avis_client import AvisAPIError, get_reservation  # noqa: E402
from session import ServicingSession, log_event  # noqa: E402

# --- Tools --------------------------------------------------------------------------
# Thin wrappers. The money-touching logic lives in extend_flow so it can be read and
# tested without an LLM in the loop.


@function_tool
def lookup_reservation(ctx: RunContextWrapper[ServicingSession], reservation_id: str) -> dict:
    """Look up an Avis reservation by ID (e.g. 'AVS-29471835').

    Reads are open, so call this first. Do not read back card, plate, or address details
    before the customer has been verified — confirm only what is needed to proceed.
    """
    session = ctx.context
    try:
        reservation = get_reservation(reservation_id.strip().upper())
    except AvisAPIError as exc:
        return extend_flow.classify_failure(session, exc, "lookup_reservation")

    session.reservation = reservation
    log_event("reservation_loaded", reservation_id=reservation.get("reservation_id"),
              status=reservation.get("status"), membership=reservation.get("membership_status"))

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
        "card_last_four": reservation.get("payment", {}).get("card_on_file", {}).get("last_four"),
    }


@function_tool
def quote_extension(ctx: RunContextWrapper[ServicingSession], new_return_datetime: str) -> dict:
    """Price an extension to a new return date/time, before anything is charged.

    Always call this and show the customer the total before asking them to confirm.
    Accepts 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM'; times are interpreted in the rental
    branch's local timezone.
    """
    session = ctx.context
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
    return extend_flow.commit_extension(ctx.context, email, cvv, billing_zip)


@function_tool
def search_policy(query: str) -> dict:
    """Search Avis policy articles for questions about rules, fees, grace periods,
    refund timing, membership benefits, and similar. Returns the most relevant articles
    with their authority level and last-updated date.

    Use this for any policy question instead of answering from memory. If it returns no
    articles, say you don't have policy on that and offer a representative — do not
    guess. Never use an article to compute a price; prices come from quote tools only.
    """
    results = kb.search(query)
    if not results:
        return {"ok": True, "articles": [],
                "note": "No policy found. Say so and offer a representative; do not improvise."}
    return {"ok": True, "articles": results}


@function_tool
def escalate_to_human(ctx: RunContextWrapper[ServicingSession], reason: str,
                      customer_intent: str, reservation_id: str = "") -> dict:
    """Hand off to a human agent, passing along everything collected so far.

    Use when the customer asks for something outside this agent's scope (including
    accepting an Avis Preferred membership upgrade), when confidence is low, or when an
    error can't be resolved by re-collecting input. The customer should never have to
    repeat information they already gave. Pass reservation_id if the customer has given
    one, even if you haven't looked it up yet.
    """
    session = ctx.context

    # A handoff is only worth anything if it carries context. If the customer gave a
    # reservation id but the flow escalated before it was ever looked up, load it here —
    # otherwise the human receives an empty envelope and re-interviews the customer,
    # which is the exact friction this handoff exists to prevent.
    if not session.reservation and reservation_id.strip():
        try:
            session.reservation = get_reservation(reservation_id.strip().upper())
        except AvisAPIError as exc:
            log_event("escalation_lookup_failed", reservation_id=reservation_id,
                      code=exc.code)

    session.escalated = True
    session.escalation_reason = reason
    session.note("customer_intent", customer_intent)
    payload = session.escalation_payload()
    log_event("escalated", reason=reason, reservation_id=session.reservation_id,
              payload=payload)
    return {"ok": True, "escalated": True, "handoff": payload,
            "customer_message":
                "I'm connecting you with a representative, and passing along everything "
                "we've covered so they can pick up where we left off."}


# --- Agent --------------------------------------------------------------------------

INSTRUCTIONS = """\
You are an Avis rental servicing agent. You help customers EXTEND an active rental —
pushing out the return date/time. That is the only change you can make yourself.

Flow, in order:
1. Get the reservation ID and look it up. Confirm you have the right rental by naming the
   vehicle and current return time. Do not read out card, license plate, or address
   details — the caller has not been verified yet.
2. Find out the new return date/time they want. Times are local to the rental branch.
3. Quote it. Always state the total clearly, in currency, and say what it covers before
   asking for agreement.
4. Get explicit agreement to that total. Then ask for the email on file plus the card's
   CVV and billing ZIP — all three are required to authorize the charge.
5. Confirm the extension and give them the confirmation number.

Rules you do not bend:
- Never charge anything the customer has not seen and agreed to. If the tool says the
  price changed, show the new total and get agreement again.
- If the price comes back different from what you quoted, say so plainly.
- You cannot cancel, modify locations, change vehicle class, or upgrade memberships. If
  asked, say so directly and offer to connect them to a representative — do not imply you
  might be able to do it.
- If a customer wants an Avis Preferred membership upgrade, you do not process it.
  Escalate with escalate_to_human so a representative can finalize it along with the
  extension in one go.
- Whenever you escalate, pass the reservation ID if the customer has given one, and
  summarize what they actually want in customer_intent. The representative should be able
  to pick up without asking them anything twice.
- When a tool returns escalate: true, stop working the request and hand off.
- Never invent prices, fees, policies, or confirmation numbers. Every number you say must
  come from a tool result.
- For policy questions (grace periods, fees, refund timing, membership benefits), use
  search_policy and answer from what it returns. If it returns nothing, say you don't
  have that policy on hand and offer a representative. Policy articles explain rules;
  they are never a source for the price of this customer's change — only quotes are.

Be warm and brief. This is a phone-style conversation, not a form.
"""

servicing_agent = Agent[ServicingSession](
    name="Avis Extend Specialist",
    model=config.AGENT_MODEL,
    instructions=INSTRUCTIONS,
    tools=[lookup_reservation, quote_extension, confirm_extension, search_policy,
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
    for attempt in range(1, max_attempts + 1):
        try:
            return Runner.run_sync(servicing_agent, user_input,
                                   context=servicing_session, session=history)
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

    print("Avis servicing agent — extend an existing rental. Ctrl-C or 'quit' to exit.\n")
    print("Agent: Hi, this is Avis support. Do you have your reservation number handy?")

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
