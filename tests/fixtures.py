"""Test fixtures: real captured payloads, minimally mutated, plus a fake transport.

Fidelity rule (DECISIONS.md): a fixture invented from scratch tests our imagination, not
the system. Every reservation fixture here starts from a payload captured live from the
mock API (tests/fixtures/*.json) and mutates the minimum needed to reach the branch under
test — one field changed, everything else authentic.

Two seams, used deliberately:
- ``fake_transport`` patches ``avis_client.requests.request`` — tests exercise the REAL
  client (retries, backoff, idempotency headers, error classification) against scripted
  responses.
- Patching client functions directly (e.g. ``avis_client.get_reservation``) skips the
  client and tests flow logic alone. Use for flow tests; never to "test" the client.
"""
from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"


# --- Reservation payloads -----------------------------------------------------------

def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def reservation(base: str = "standard", **overrides) -> dict:
    """A deep copy of a captured payload with dotted-path overrides applied.

    reservation(status="cancelled")
    reservation(**{"dates.pickup_datetime": "2027-01-01T10:00:00-05:00"})
    Use value ``DELETE`` to remove a key entirely (the malformed cases).
    """
    payload = copy.deepcopy(_load(f"reservation_{base}"))
    for dotted, value in overrides.items():
        node = payload
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        if value is DELETE:
            node.pop(leaf, None)
        else:
            node[leaf] = value
    return payload


DELETE = object()

# All the "2026-06-xx" fixture dates predate this. Tests that call build_quote directly
# must pass now=EXTEND_TEST_NOW so they stay deterministic regardless of when they
# actually run — real wall-clock time will eventually pass those fixed dates too, which
# is exactly the class of bug the real-time floor in build_quote exists to catch
# (REVIEW_QUEUE #17). Pinning `now` keeps the test suite from becoming its own time bomb.
EXTEND_TEST_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _shift_iso(iso: str, **delta) -> str:
    dt = datetime.fromisoformat(iso)
    return (dt + timedelta(**delta)).isoformat()


def reservation_pre_pickup(hours_until_pickup: float, base: str = "standard") -> dict:
    """A reservation whose pickup is in the future — the branch no test account has.

    Anchored to *now* so the 48h cancellation window is exercised relative to real time,
    the same way the policy logic will compute it.
    """
    payload = reservation(base)
    offset = payload["dates"]["pickup_datetime"][-6:]
    tz = datetime.now(timezone.utc).astimezone().tzinfo
    pickup = datetime.now(tz) + timedelta(hours=hours_until_pickup)
    rental_days = 3
    payload["dates"]["pickup_datetime"] = pickup.isoformat(timespec="seconds")
    payload["dates"]["current_return_datetime"] = (pickup + timedelta(days=rental_days)).isoformat(timespec="seconds")
    payload["dates"]["original_return_datetime"] = payload["dates"]["current_return_datetime"]
    payload["status"] = "active"
    return payload


def reservation_pay_at_counter(base: str = "standard") -> dict:
    """No prepayment: kb_can_02 says there is simply nothing to refund."""
    return reservation(base, **{"payment.total_charged": 0.0})


# --- Error envelopes (documented shape; codes captured from real responses) ---------

def error_body(code: str, message: str = "", details: dict | None = None) -> dict:
    return {"success": False,
            "error": {"code": code, "message": message or code, "details": details or {}}}


# --- Fake transport over the real client --------------------------------------------

class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body
        self.ok = 200 <= status < 300
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body


class Timeout(Exception):
    """Marker: place in a script to raise requests.Timeout on that attempt."""


class ConnectionError_(Exception):
    """Marker: place in a script to raise requests.ConnectionError on that attempt."""


@contextmanager
def fake_transport(script: list):
    """Patch avis_client's HTTP layer with a scripted sequence of outcomes.

    Each entry is consumed by one attempt: a FakeResponse, or the Timeout /
    ConnectionError_ marker classes. Records every attempt's (method, url, headers,
    json) so tests can assert on retry counts and header stability.

        with fake_transport([Timeout, FakeResponse(503, ...), FakeResponse(200, ok)]) as calls:
            result = avis_client.extend_reservation(...)
        assert len(calls) == 3
    """
    import requests as real_requests

    import avis_client

    calls: list[dict] = []
    remaining = list(script)

    def scripted(method, url, headers=None, params=None, json=None, timeout=None):
        calls.append({"method": method, "url": url, "headers": dict(headers or {}),
                      "params": params, "json": json})
        if not remaining:
            raise AssertionError("fake_transport script exhausted — unexpected extra attempt")
        step = remaining.pop(0)
        if step is Timeout:
            raise real_requests.Timeout("scripted timeout")
        if step is ConnectionError_:
            raise real_requests.ConnectionError("scripted connection error")
        return step

    original = avis_client.requests.request
    avis_client.requests.request = scripted
    try:
        yield calls
    finally:
        avis_client.requests.request = original
