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
    accepted: bool = False

    def age_seconds(self) -> float:
        return time.monotonic() - self.quoted_at


@dataclass
class ServicingSession:
    """Per-conversation state. One instance per customer interaction."""

    reservation: dict | None = None
    verified_email: str | None = None
    pending_quote: PendingQuote | None = None
    failed_verifications: int = 0
    escalated: bool = False
    escalation_reason: str | None = None
    # Anything worth handing a human: quoted options, offers made, customer intent.
    collected_context: dict = field(default_factory=dict)

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
        current = (self.reservation or {}).get("dates", {}).get("current_return_datetime", "")
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
            "collected_context": self.collected_context,
        }
