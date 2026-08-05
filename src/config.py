"""Feature flags and tunables (see DECISIONS.md §4).

Every speculative product behavior is a flag, defaulted off where it is commercially
aggressive or unvalidated, and shadow-logged even when off so the counterfactual can be
measured before anything is exposed to a real customer.
"""
from __future__ import annotations

import os


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


# --- Speculative behaviors (see DECISIONS.md §4 flag table) -------------------------

# Quote nearby return dates alongside the requested one and surface materially cheaper
# options. Three modes, because unlike the other flags this one's counterfactual is not
# free — computing "what would we have offered" means really calling /quote:
#   off    — don't compute, don't surface (default)
#   shadow — compute and log the counterfactual, never surface it to the customer
#   on     — compute and surface
# Default off: the revenue effect is unvalidated and it multiplies traffic on the API's
# flakiest path. Run `shadow` to gather the data that would justify `on`.
FLEXIBLE_DATE_ALTERNATIVES_MODE = os.environ.get("FLEXIBLE_DATE_ALTERNATIVES_MODE", "off").strip().lower()

# Surface an Avis Preferred membership upgrade inside extend/modify where it would
# offset cost (Preferred members are exempt from the $29 late fee). Acceptance escalates
# to a human — the agent never calls the upgrade endpoint itself.
IN_FLOW_UPGRADE_OFFER = _flag("IN_FLOW_UPGRADE_OFFER", False)

# Offer a date change once before processing a cancellation.
CANCEL_RETENTION_PROMPT = _flag("CANCEL_RETENTION_PROMPT", False)


# --- Tunables ----------------------------------------------------------------------

# Only surface an alternative date if it saves at least max(ABS, PCT * quote total).
ALTERNATIVE_MATERIALITY_ABS_USD = float(os.environ.get("ALTERNATIVE_MATERIALITY_ABS_USD", "15"))
ALTERNATIVE_MATERIALITY_PCT = float(os.environ.get("ALTERNATIVE_MATERIALITY_PCT", "0.10"))
MAX_ALTERNATIVES = 2

# A quote older than this is re-priced before a write, and any delta is surfaced to the
# customer rather than silently charged (DECISIONS.md §5, quote staleness).
QUOTE_TTL_SECONDS = float(os.environ.get("QUOTE_TTL_SECONDS", "120"))

# Escalate after this many failed email verifications on a write.
MAX_VERIFICATION_ATTEMPTS = int(os.environ.get("MAX_VERIFICATION_ATTEMPTS", "3"))

# Known facts used only for shadow-logging the upgrade offer's value; the API and the
# knowledge base remain the sources of truth for anything shown to a customer.
LATE_RETURN_FEE_USD = 29.00

AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4.1")
