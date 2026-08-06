"""Bounded Hypothesis strategies derived from captured API fixtures.

The values here deliberately mutate only the field under test.  The surrounding
reservation always comes from ``tests/fixtures/reservation_standard.json`` so these
tests explore variations of an observed payload, not an invented API shape.
"""
from __future__ import annotations

from hypothesis import strategies as st


REQUIRED_RESERVATION_PATHS = (
    "reservation_id",
    "status",
    "customer_id",
    "customer_name",
    "membership_status",
    "dates.pickup_datetime",
    "dates.current_return_datetime",
    "pricing.daily_rate",
    "payment.total_charged",
)

required_reservation_paths = st.sampled_from(REQUIRED_RESERVATION_PATHS)

# Keep generated money realistic and exactly representable as business-facing cents.
money_cents = st.integers(min_value=0, max_value=250_000)
positive_money_cents = st.integers(min_value=1, max_value=250_000)

# Each item is one documented transient transport outcome.  Limiting this to three
# keeps the successful response within the client's four-attempt retry budget.
transient_prefixes = st.lists(
    st.sampled_from(("timeout", "connection", "http_500", "http_503")),
    min_size=0,
    max_size=3,
)
