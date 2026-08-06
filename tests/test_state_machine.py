"""Rule-based tests for the servicing session's money-moving state transitions.

Hypothesis explores sequences rather than isolated examples here: reservation switches,
quotes and estimates, repeated confirmations, repricing, early-return resolution, and
terminal handoffs can be interleaved in orders that are tedious to enumerate manually.

The HTTP and LLM boundaries are replaced with deterministic stubs.  The state machine
still calls the public agent tools for customer-visible actions, so the hard-handoff gate
and the real extend/cancel flow code remain under test.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from hypothesis import settings, strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import avis_client  # noqa: E402
import cancel_flow  # noqa: E402
import config  # noqa: E402
import extend_flow  # noqa: E402
from fixtures import reservation  # noqa: E402
from session import PendingCancellation, PendingQuote, ServicingSession  # noqa: E402


QUOTE_TOTAL = 159.47
QUOTE_CHARGES = {
    "daily_rate": 38.99,
    "extension_days": 3,
    "subtotal": 116.97,
    "late_fee": 29.0,
    "one_way_fee": 0.0,
    "taxes_and_fees": 13.5,
    "total_charged": QUOTE_TOTAL,
    "currency": "USD",
}
# Deliberately far beyond the captured fixture's dates and real wall-clock time.  Pricing
# is stubbed, so the length of the requested extension is immaterial to these gate tests.
FUTURE_RETURN = "2099-06-17T15:00:00-05:00"
EMAIL = "state-machine@example.com"


class _Ctx:
    def __init__(self, session: ServicingSession):
        self.context = session


def _call(tool, session: ServicingSession, **kwargs):
    """Invoke the implementation wrapped by ``@function_tool``."""
    return tool.__wrapped__(_Ctx(session), **kwargs)


class ServicingLifecycleMachine(RuleBasedStateMachine):
    """Exercise the session as a small safety state machine.

    The fake writes record the exact staged object and turn that authorized them.  This
    makes the strongest assertions independent of the outcome returned by the flow: a
    write stub being reached at all is proof that the money boundary was crossed.
    """

    def __init__(self):
        super().__init__()
        self.session = ServicingSession()

        self.quote_total = QUOTE_TOTAL
        self.extension_actual_delta = 0.0
        self.cancel_outcome = "matching"
        self.extension_writes: list[tuple[PendingQuote, int, int]] = []
        self.cancellation_writes: list[tuple[PendingCancellation, int, int]] = []
        self.ever_hard_handoff = False

        self._originals = {
            "quote_change": extend_flow.quote_change,
            "extend_reservation": extend_flow.extend_reservation,
            "cancel_reservation": cancel_flow.cancel_reservation,
            "get_reservation": avis_client.get_reservation,
            "alternatives_mode": config.FLEXIBLE_DATE_ALTERNATIVES_MODE,
            "quote_ttl": config.QUOTE_TTL_SECONDS,
        }
        config.FLEXIBLE_DATE_ALTERNATIVES_MODE = "off"
        config.QUOTE_TTL_SECONDS = 120
        extend_flow.quote_change = self._fake_quote
        extend_flow.extend_reservation = self._fake_extend
        cancel_flow.cancel_reservation = self._fake_cancel
        avis_client.get_reservation = lambda reservation_id: reservation(
            "preferred" if reservation_id == "AVS-29471835" else "standard"
        )

    def teardown(self):
        extend_flow.quote_change = self._originals["quote_change"]
        extend_flow.extend_reservation = self._originals["extend_reservation"]
        cancel_flow.cancel_reservation = self._originals["cancel_reservation"]
        avis_client.get_reservation = self._originals["get_reservation"]
        config.FLEXIBLE_DATE_ALTERNATIVES_MODE = self._originals["alternatives_mode"]
        config.QUOTE_TTL_SECONDS = self._originals["quote_ttl"]
        super().teardown()

    # --- deterministic boundaries -------------------------------------------------

    def _fake_quote(self, *_args, **_kwargs):
        charges = dict(QUOTE_CHARGES, total_charged=self.quote_total)
        return {"success": True, "quote": {"charges": charges}}

    def _fake_extend(self, *_args, **_kwargs):
        staged = self.session.pending_quote
        assert staged is not None, "extension API reached without a staged quote"
        assert not any(previous is staged for previous, _, _ in self.extension_writes), (
            "the same quote object authorized more than one extension write"
        )
        self.extension_writes.append((staged, staged.quoted_on_turn, self.session.turn))
        total = round(float(staged.total_charged) + self.extension_actual_delta, 2)
        return {
            "success": True,
            "confirmation_number": f"EXT-SM-{len(self.extension_writes)}",
            "extension_details": {"extension_days": 3, "late_return": True},
            "charges": dict(staged.charges, total_charged=total),
        }

    def _fake_cancel(self, *_args, **_kwargs):
        staged = self.session.pending_cancellation
        assert staged is not None, "cancel API reached without a staged estimate"
        assert not any(previous is staged for previous, _, _ in self.cancellation_writes), (
            "the same cancellation estimate authorized more than one write"
        )
        self.cancellation_writes.append((staged, staged.quoted_on_turn, self.session.turn))

        penalty = staged.penalty_estimate
        refund = staged.refund_estimate
        if self.cancel_outcome == "subthreshold":
            penalty += config.CANCEL_VARIANCE_THRESHOLD_USD / 2
        elif self.cancel_outcome == "adverse_penalty":
            penalty += config.CANCEL_VARIANCE_THRESHOLD_USD
        elif self.cancel_outcome == "adverse_refund":
            refund = max(0.0, refund - config.CANCEL_VARIANCE_THRESHOLD_USD)
        elif self.cancel_outcome == "favorable":
            penalty = max(0.0, penalty - config.CANCEL_VARIANCE_THRESHOLD_USD)
            refund += config.CANCEL_VARIANCE_THRESHOLD_USD

        return {
            "success": True,
            "confirmation_number": f"CXL-SM-{len(self.cancellation_writes)}",
            "cancellation_details": {
                "penalty": round(penalty, 2),
                "refund_amount": round(refund, 2),
                "prepaid_amount": staged.prepaid,
                "currency": staged.currency,
            },
        }

    # --- externally visible transitions ------------------------------------------

    def _load(self, base: str) -> None:
        previous_id = self.session.reservation_id
        incoming = reservation(base)
        switching = previous_id is not None and incoming["reservation_id"] != previous_id
        was_handed_off = self.session.handed_off
        self.session.load_reservation(incoming)

        if switching:
            assert self.session.verified_email is None
            assert self.session.pending_quote is None
            assert self.session.pending_cancellation is None
            assert self.session.failed_verifications == 0
        if was_handed_off:
            assert self.session.handed_off, "reservation switch reopened a terminal handoff"

    @rule()
    def load_standard_reservation(self):
        self._load("standard")

    @rule()
    def load_preferred_reservation(self):
        self._load("preferred")

    @rule()
    def advance_customer_turn(self):
        self.session.turn += 1

    @precondition(lambda self: self.session.reservation is not None)
    @rule()
    def quote_extension(self):
        before = len(self.extension_writes)
        self.quote_total = QUOTE_TOTAL
        outcome = _call(
            agent.quote_extension,
            self.session,
            new_return_datetime=FUTURE_RETURN,
        )
        if self.session.handed_off:
            assert outcome.get("handed_off") is True
            assert len(self.extension_writes) == before
        else:
            assert outcome["ok"] is True, outcome
            assert self.session.pending_quote is not None
            assert self.session.pending_quote.quoted_on_turn == self.session.turn

    @precondition(
        lambda self: self.session.pending_quote is not None
        and not self.session.pending_quote.consumed
        and not self.session.handed_off
    )
    @rule()
    def reprice_extension(self):
        """A changed total becomes a fresh, same-turn quote requiring new consent."""
        staged = self.session.pending_quote
        old_total = float(staged.total_charged)
        self.quote_total = round(old_total + 5.0, 2)
        staged.quoted_at = time.monotonic() - config.QUOTE_TTL_SECONDS - 1
        outcome = extend_flow.revalidate_quote(self.session)
        assert outcome and outcome["repriced"] is True
        assert staged.total_charged == self.quote_total
        assert staged.quoted_on_turn == self.session.turn

        writes_before = len(self.extension_writes)
        blocked = _call(
            agent.confirm_extension,
            self.session,
            email=EMAIL,
            cvv="123",
            billing_zip="60601",
        )
        assert blocked["ok"] is False
        assert len(self.extension_writes) == writes_before, (
            "a repriced total was written before another customer turn"
        )

    @precondition(lambda self: self.session.reservation is not None)
    @rule(actual_delta=st.sampled_from([0.0, -5.0, 0.005, 5.0]))
    def confirm_extension(self, actual_delta: float):
        before = len(self.extension_writes)
        staged = self.session.pending_quote
        already_consumed = bool(staged and staged.consumed)
        same_turn = bool(staged and self.session.turn <= staged.quoted_on_turn)
        was_handed_off = self.session.handed_off
        self.extension_actual_delta = actual_delta

        outcome = _call(
            agent.confirm_extension,
            self.session,
            email=EMAIL,
            cvv="123",
            billing_zip="60601",
        )
        if was_handed_off:
            assert outcome.get("handed_off") is True
            assert len(self.extension_writes) == before
        elif staged is None or already_consumed or same_turn:
            assert outcome["ok"] is False
            assert len(self.extension_writes) == before
        else:
            assert outcome["ok"] is True
            assert len(self.extension_writes) == before + 1
            if actual_delta >= config.EXTEND_VARIANCE_THRESHOLD_USD:
                assert outcome.get("escalate") is True
                assert self.session.handed_off is True
                self.ever_hard_handoff = True
            else:
                assert "escalate" not in outcome

    @precondition(lambda self: self.session.reservation is not None)
    @rule()
    def estimate_cancellation(self):
        before = len(self.cancellation_writes)
        outcome = _call(agent.estimate_cancellation, self.session)
        if self.session.handed_off:
            assert outcome.get("handed_off") is True
            assert len(self.cancellation_writes) == before
        else:
            assert outcome["ok"] is True, outcome
            assert self.session.pending_cancellation is not None
            assert self.session.pending_cancellation.quoted_on_turn == self.session.turn

    @precondition(
        lambda self: self.session.pending_cancellation is not None
        and not self.session.handed_off
    )
    @rule()
    def resolve_as_true_cancellation(self):
        outcome = _call(
            agent.resolve_cancel_intent,
            self.session,
            intent=cancel_flow.TRUE_CANCELLATION,
        )
        assert outcome["ok"] is True
        assert self.session.pending_cancellation.intent_confirmed is True

    @precondition(
        lambda self: self.session.pending_cancellation is not None
        and not self.session.handed_off
    )
    @rule()
    def resolve_as_early_return(self):
        before = len(self.cancellation_writes)
        outcome = _call(
            agent.resolve_cancel_intent,
            self.session,
            intent=cancel_flow.EARLY_RETURN,
        )
        assert outcome["ok"] is True and outcome["nothing_cancelled"] is True
        assert self.session.pending_cancellation is None

        # A later accidental confirmation still has no staged authority to spend.
        self.session.turn += 1
        follow_up = _call(
            agent.confirm_cancellation,
            self.session,
            email=EMAIL,
            reason="early return was already selected",
        )
        assert follow_up["ok"] is False
        assert len(self.cancellation_writes) == before

    @precondition(lambda self: self.session.reservation is not None)
    @rule(
        result=st.sampled_from(
            ["matching", "subthreshold", "adverse_penalty", "adverse_refund", "favorable"]
        )
    )
    def confirm_cancellation(self, result: str):
        before = len(self.cancellation_writes)
        staged = self.session.pending_cancellation
        already_consumed = bool(staged and staged.consumed)
        same_turn = bool(staged and self.session.turn <= staged.quoted_on_turn)
        unresolved = bool(staged and staged.requires_disambiguation and not staged.intent_confirmed)
        was_handed_off = self.session.handed_off
        self.cancel_outcome = result

        outcome = _call(
            agent.confirm_cancellation,
            self.session,
            email=EMAIL,
            reason="state-machine test",
        )
        if was_handed_off:
            assert outcome.get("handed_off") is True
            assert len(self.cancellation_writes) == before
        elif staged is None or already_consumed or same_turn or unresolved:
            assert outcome["ok"] is False
            assert len(self.cancellation_writes) == before
        else:
            assert outcome["ok"] is True
            assert len(self.cancellation_writes) == before + 1
            if result in {"adverse_penalty", "adverse_refund"}:
                assert outcome.get("escalate") is True
                assert self.session.handed_off is True
                self.ever_hard_handoff = True
            else:
                assert "escalate" not in outcome

    @rule()
    def hard_handoff(self):
        self.session.escalated = True
        self.session.handed_off = True
        self.session.escalation_reason = "state-machine terminal handoff"
        self.ever_hard_handoff = True

    @precondition(lambda self: self.session.handed_off)
    @rule()
    def retry_actions_after_handoff(self):
        """Every reservation-scoped tool must refuse without crossing a boundary."""
        extension_before = len(self.extension_writes)
        cancellation_before = len(self.cancellation_writes)
        cases = [
            (agent.lookup_reservation, {"reservation_id": "AVS-29471835"}),
            (agent.quote_extension, {"new_return_datetime": FUTURE_RETURN}),
            (
                agent.confirm_extension,
                {"email": EMAIL, "cvv": "123", "billing_zip": "60601"},
            ),
            (agent.estimate_cancellation, {}),
            (agent.resolve_cancel_intent, {"intent": cancel_flow.TRUE_CANCELLATION}),
            (agent.confirm_cancellation, {"email": EMAIL, "reason": "retry"}),
        ]
        for tool, kwargs in cases:
            outcome = _call(tool, self.session, **kwargs)
            assert outcome.get("handed_off") is True, tool.name
            assert outcome["ok"] is False, tool.name
        assert len(self.extension_writes) == extension_before
        assert len(self.cancellation_writes) == cancellation_before

    # --- always-on safety properties ----------------------------------------------

    @invariant()
    def every_write_happens_after_the_staged_turn(self):
        for _, quoted_on_turn, write_turn in self.extension_writes:
            assert write_turn > quoted_on_turn
        for _, quoted_on_turn, write_turn in self.cancellation_writes:
            assert write_turn > quoted_on_turn

    @invariant()
    def staged_authority_is_single_use(self):
        extension_objects = [quote for quote, _, _ in self.extension_writes]
        cancellation_objects = [estimate for estimate, _, _ in self.cancellation_writes]
        assert all(
            sum(candidate is quote for candidate in extension_objects) == 1
            for quote in extension_objects
        )
        assert all(
            sum(candidate is estimate for candidate in cancellation_objects) == 1
            for estimate in cancellation_objects
        )

    @invariant()
    def terminal_handoff_never_reopens(self):
        if self.ever_hard_handoff:
            assert self.session.handed_off is True


TestServicingLifecycle = ServicingLifecycleMachine.TestCase
TestServicingLifecycle.settings = settings(
    max_examples=35,
    stateful_step_count=25,
    deadline=None,
)
