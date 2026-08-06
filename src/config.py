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

# Total wall-clock budget for the speculative alternative quotes. They are a nicety; the
# extension the customer actually asked for must never wait on them.
ALTERNATIVE_QUOTE_BUDGET_S = float(os.environ.get("ALTERNATIVE_QUOTE_BUDGET_S", "4"))

# A quote older than this is re-priced before a write, and any delta is surfaced to the
# customer rather than silently charged (DECISIONS.md §5, quote staleness).
QUOTE_TTL_SECONDS = float(os.environ.get("QUOTE_TTL_SECONDS", "120"))

# If the actual cancellation outcome is ADVERSE to the customer versus the estimate they
# agreed to by at least this much (higher penalty or smaller refund), escalate to a human.
# Asymmetric on purpose: a pleasant surprise is reported, not escalated — bothering a rep
# with good news wastes the handoff. Kept low because the estimator matched the API to the
# cent on every observed reservation; any real deviation means an unobserved policy branch,
# which is exactly when a person should look. (DECISIONS.md §3, handoff rules.)
CANCEL_VARIANCE_THRESHOLD_USD = float(os.environ.get("CANCEL_VARIANCE_THRESHOLD_USD", "1.00"))

# The same check on the extend path, kept as a separate knob because the two mean
# different things. Cancel's estimate is KB-derived, so a variance means an unobserved
# policy branch. Extend's figure came from the API's OWN /quote minutes earlier, so any
# variance at all means the API repriced between quote and write — a more serious signal
# about the integration, and one Avis would want reported at a tighter threshold.
EXTEND_VARIANCE_THRESHOLD_USD = float(os.environ.get("EXTEND_VARIANCE_THRESHOLD_USD", "0.01"))

# Escalate after this many failed email verifications on a write.
MAX_VERIFICATION_ATTEMPTS = int(os.environ.get("MAX_VERIFICATION_ATTEMPTS", "3"))

# Known facts used only for shadow-logging the upgrade offer's value; the API and the
# knowledge base remain the sources of truth for anything shown to a customer.
LATE_RETURN_FEE_USD = 29.00

AGENT_MODEL = os.environ.get("AGENT_MODEL", "gpt-4.1")
