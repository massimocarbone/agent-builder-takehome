"""Shared conversation state — the handoff contract between agents.

Held outside the model and passed as run context, so the parts that gate money are
enforced structurally rather than by prompt compliance. Two things depend on this:

- **The confirmation gate.** A write tool refuses unless a quote the customer actually
  saw is sitting in ``pending_quote``. The model cannot talk its way past that.
- **Escalation to a human.** When the agent hands off, everything it collected travels
  with it (``escalation_payload``) so the customer never repeats themselves — which is
  the whole point of the upgrade-upsell decision in DECISIONS.md §3.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).resolve().parent.parent / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

_agent_logger = logging.getLogger("avis_agent")
if not _agent_logger.handlers:
    _agent_logger.setLevel(logging.INFO)
    _h = logging.FileHandler(LOG_DIR / "agent.jsonl")
    _h.setFormatter(logging.Formatter("%(message)s"))
    _agent_logger.addHandler(_h)


def log_event(event: str, **fields: Any) -> None:
    """Append a structured decision/outcome record to logs/agent.jsonl."""
    fields["event"] = event
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _agent_logger.info(json.dumps(fields, default=str))


@dataclass
class PendingQuote:
    """A price the customer has been shown, awaiting confirmation."""

    change_type: str
    new_return_datetime: str
    total_charged: float
    currency: str
    charges: dict
    quoted_at: float = field(default_factory=time.monotonic)
    # The conversation turn this quote was created on. A write is only permitted on a
    # LATER turn, which is what "the customer saw this number and replied" means
    # structurally — the model cannot quote and charge inside a single turn.
    quoted_on_turn: int = 0
    accepted: bool = False
    # Set once the write succeeds, so a retried tool call or a replayed turn cannot
    # charge a second time. Each write generates a fresh idempotency key, so the API's
    # replay protection does not cover this case — only refusing here does.
    consumed: bool = False

    def age_seconds(self) -> float:
        return time.monotonic() - self.quoted_at


# Customers type card codes straight into chat ("cvv is 847"). The transcript is handed to
# a human and written to logs, so scrub the obvious secrets first. Best-effort by nature —
# free text can always hide a number somewhere this misses.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(cvv|cvc|security code)\b\D{0,10}(\d{3,4})"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # card-like digit runs
]


def redact_text(text: str) -> str:
    out = _SECRET_PATTERNS[0].sub(lambda m: f"{m.group(1)} ***", text or "")
    return _SECRET_PATTERNS[1].sub("***", out)


@dataclass
class PendingCancellation:
    """A cancellation estimate the customer has been shown, awaiting confirmation.

    Same gate semantics as PendingQuote: staged on one turn, committable only on a later
    one, single-use. The figures are policy-derived ESTIMATES (there is no cancel quote
    endpoint), which is why the actual API outcome is compared against them post-write.
    """

    branch: str
    penalty_estimate: float
    refund_estimate: float
    prepaid: float
    currency: str
    citation: dict
    caveats: list[str]
    quoted_on_turn: int = 0
    consumed: bool = False
    quoted_at: float = field(default_factory=time.monotonic)


@dataclass
class ServicingSession:
    """Per-conversation state. One instance per customer interaction."""

    reservation: dict | None = None
    verified_email: str | None = None
    pending_quote: PendingQuote | None = None
    pending_cancellation: PendingCancellation | None = None
    failed_verifications: int = 0
    escalated: bool = False
    escalation_reason: str | None = None
    # Terminal handoff. Distinct from `escalated`: an *assistive* escalation ("I can't help
    # you find your reservation id") should stay revocable — customers routinely resolve
    # the blocker a turn later. A *hard* handoff (verification failure, terminal API error,
    # an action this agent must not take) sets this, and action tools then refuse. The
    # agent going quiet is enforced here, not requested in the prompt.
    handed_off: bool = False
    # Incremented once per customer turn. The confirmation gate compares this against the
    # turn a quote was staged on, so consent is measured in conversation turns rather than
    # wall-clock time (which a fast customer or a slow test would each trip the wrong way).
    turn: int = 0
    # Verbatim turns, secrets scrubbed. A human receives this rather than only the model's
    # own summary of events — the summary is the least trustworthy thing in the payload.
    transcript: list[dict] = field(default_factory=list)
    # Anything worth handing a human: quoted options, offers made, customer intent.
    collected_context: dict = field(default_factory=dict)

    def record(self, role: str, text: str) -> None:
        self.transcript.append({"role": role, "text": redact_text(text)})

    def load_reservation(self, reservation: dict) -> None:
        """Install a reservation, clearing anything scoped to a *different* one.

        Verification, a staged quote, and the failed-attempt count are all facts about one
        booking. Carrying them across a switch means proving you know reservation A's email
        unlocks reservation B's card digits, and a quote priced for A can be committed
        against B. `handed_off` is deliberately NOT reset — a terminated conversation must
        not reopen just because the caller names another reservation.
        """
        incoming = (reservation or {}).get("reservation_id")
        if incoming != self.reservation_id:
            if self.reservation_id is not None:
                log_event("reservation_switched", previous=self.reservation_id,
                          new=incoming, cleared_verification=bool(self.verified_email),
                          cleared_quote=self.pending_quote is not None)
            self.verified_email = None
            self.pending_quote = None
            self.pending_cancellation = None
            self.failed_verifications = 0
        self.reservation = reservation

    # --- Convenience accessors ------------------------------------------------------

    @property
    def reservation_id(self) -> str | None:
        return (self.reservation or {}).get("reservation_id")

    @property
    def is_preferred(self) -> bool:
        return (self.reservation or {}).get("membership_status") == "avis_preferred"

    @property
    def return_utc_offset(self) -> str:
        """The pickup branch's UTC offset, taken from the reservation's own datetimes.

        Rental times are local to the branch; deriving the offset from live data avoids
        the off-by-one-day errors that come from assuming a timezone.
        """
        current = (self.reservation or {}).get("dates", {}).get("current_return_datetime") or ""
        return current[-6:] if len(current) >= 6 and current[-6] in "+-" else "+00:00"

    def note(self, key: str, value: Any) -> None:
        """Record something worth carrying into a human handoff."""
        self.collected_context[key] = value

    def escalation_payload(self) -> dict:
        """Everything a human agent needs to finish without re-interviewing the customer."""
        reservation = self.reservation or {}
        return {
            "reason": self.escalation_reason,
            "reservation_id": self.reservation_id,
            "customer_name": reservation.get("customer_name"),
            "customer_id": reservation.get("customer_id"),
            "membership_status": reservation.get("membership_status"),
            "email_verified": bool(self.verified_email),
            "current_return_datetime": reservation.get("dates", {}).get("current_return_datetime"),
            "pending_quote": asdict(self.pending_quote) if self.pending_quote else None,
            "pending_cancellation": (asdict(self.pending_cancellation)
                                     if self.pending_cancellation else None),
            "collected_context": self.collected_context,
            "transcript": self.transcript,
        }
