"""Deterministic Avis API client — Layer 1 of the architecture (see DECISIONS.md §2).

Typed function per endpoint; the LLM never constructs requests freeform. Handles the
API's documented imperfections:

- Retry with exponential backoff on transient failures (5xx, timeouts, connection
  errors). Terminal errors (4xx) are never retried.
- Writes send an ``Idempotency-Key`` header, generated once per logical operation and
  reused across retries, so a retry after a lost response replays the original result
  instead of double-acting.
- Every attempt is logged as a structured JSONL line to ``logs/api.jsonl`` (secrets
  redacted).

Errors surface as ``AvisAPIError`` carrying the API's error code, HTTP status, and
whether the failure was transient — callers (agent tools) branch on that, not on raw
requests exceptions.

Run directly to smoke-test connectivity:
    python src/avis_client.py
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

AVIS_API_URL = os.environ["AVIS_API_URL"].rstrip("/")
AVIS_API_KEY = os.environ["AVIS_API_KEY"]

REQUEST_TIMEOUT_S = float(os.environ.get("AVIS_TIMEOUT_S", "15"))
MAX_ATTEMPTS = int(os.environ.get("AVIS_MAX_ATTEMPTS", "4"))
BACKOFF_BASE_S = 0.5

LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).resolve().parent.parent / "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

_REDACTED_FIELDS = {"cvv", "billing_zip", "email"}

logger = logging.getLogger("avis_client")
if not logger.handlers:
    logger.setLevel(logging.INFO)
    _handler = logging.FileHandler(LOG_DIR / "api.jsonl")
    _handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_handler)


class AvisAPIError(Exception):
    """A non-2xx or failed request, classified for callers.

    Attributes:
        code: the API's string error code (e.g. VERIFICATION_FAILED), or a synthetic
            one for transport failures (TIMEOUT, CONNECTION_ERROR, EXHAUSTED_RETRIES).
        status: HTTP status code, or None for transport failures.
        retryable: True if the failure was transient (the client already retried;
            this tells the caller the operation *may* have another chance later).
        details: the API error envelope's details object, if any.
    """

    def __init__(self, code: str, message: str, status: int | None = None,
                 retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.details = details or {}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if k in _REDACTED_FIELDS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def _log(**fields: Any) -> None:
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    logger.info(json.dumps(fields, default=str))


def _request(method: str, path: str, *, params: dict | None = None,
             body: dict | None = None, idempotent_write: bool = False) -> dict:
    """Perform a request with retry/backoff and structured logging.

    Retries only transient failures: 5xx, timeouts, connection errors. 4xx responses
    raise immediately with the API's error code. Writes get a stable Idempotency-Key
    so retries are safe.
    """
    url = f"{AVIS_API_URL}{path}"
    headers = {"X-API-Key": AVIS_API_KEY}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if idempotent_write:
        headers["Idempotency-Key"] = str(uuid.uuid4())

    last_error: AvisAPIError | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started = time.monotonic()
        outcome, status = "ok", None
        try:
            resp = requests.request(method, url, headers=headers, params=params,
                                    json=body, timeout=REQUEST_TIMEOUT_S)
            status = resp.status_code
            if resp.ok:
                return resp.json()

            try:
                envelope = resp.json().get("error", {})
            except ValueError:
                envelope = {}
            code = envelope.get("code", f"HTTP_{status}")
            message = envelope.get("message", resp.text[:200])
            transient = status >= 500
            outcome = f"error:{code}"
            last_error = AvisAPIError(code, message, status=status,
                                      retryable=transient, details=envelope.get("details"))
            if not transient:
                raise last_error
        except requests.Timeout:
            outcome = "timeout"
            last_error = AvisAPIError("TIMEOUT", f"{method} {path} timed out after {REQUEST_TIMEOUT_S}s",
                                      retryable=True)
        except requests.ConnectionError as exc:
            outcome = "connection_error"
            last_error = AvisAPIError("CONNECTION_ERROR", str(exc), retryable=True)
        finally:
            _log(endpoint=f"{method} {path}", params=_redact(params), body=_redact(body),
                 status=status, attempt=attempt, latency_ms=round((time.monotonic() - started) * 1000),
                 outcome=outcome)

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))

    raise AvisAPIError("EXHAUSTED_RETRIES",
                       f"{method} {path} failed after {MAX_ATTEMPTS} attempts: {last_error}",
                       status=last_error.status if last_error else None,
                       retryable=True,
                       details=last_error.details if last_error else None)


# --- Payload validation -------------------------------------------------------------

# Fields every downstream flow depends on. Validated once, here at the boundary, so the
# flows never carry defensive .get() chains — a payload that reaches them is well-formed.
_REQUIRED_RESERVATION_FIELDS = [
    ("reservation_id",),
    ("status",),
    ("customer_id",),
    ("customer_name",),
    ("membership_status",),
    ("dates", "pickup_datetime"),
    ("dates", "current_return_datetime"),
    ("pricing", "daily_rate"),
    ("payment", "total_charged"),
]


def validate_reservation(payload: dict) -> dict:
    """Check the fields downstream logic relies on; raise MALFORMED_RESERVATION if not.

    The real API has never sent a malformed reservation — this guards against the day it
    does. A classified terminal error here becomes a graceful escalation with a full
    handoff payload; an unguarded KeyError three layers up becomes a dead-end apology.
    """
    missing = []
    for path in _REQUIRED_RESERVATION_FIELDS:
        node = payload
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
            if node is None:
                missing.append(".".join(path))
                break
    if missing:
        raise AvisAPIError(
            "MALFORMED_RESERVATION",
            f"Reservation payload missing required fields: {', '.join(missing)}",
            retryable=False,
            details={"missing_fields": missing},
        )
    return payload


# --- Reads (open; only the API key) -------------------------------------------------

def get_reservation(reservation_id: str) -> dict:
    """Look up a reservation. Raises AvisAPIError (RESERVATION_NOT_FOUND on bad id,
    MALFORMED_RESERVATION if the payload is missing fields downstream logic needs)."""
    return validate_reservation(_request("GET", f"/reservations/{reservation_id}"))


def get_availability(location: str, vehicle_type: str, start_date: str, end_date: str) -> dict:
    """Check availability at a location for a YYYY-MM-DD date range."""
    return _request("GET", "/availability", params={
        "location": location, "vehicle_type": vehicle_type,
        "start_date": start_date, "end_date": end_date,
    })


def quote_change(reservation_id: str, change_type: str, new_return_datetime: str,
                 new_return_location: str | None = None) -> dict:
    """Price a proposed extend/modify. No side effects; safe to call speculatively."""
    body: dict[str, Any] = {"change_type": change_type, "new_return_datetime": new_return_datetime}
    if new_return_location:
        body["new_return_location"] = new_return_location
    return _request("POST", f"/reservations/{reservation_id}/quote", body=body)


# --- Writes (email verification; idempotency-keyed) ---------------------------------

def extend_reservation(reservation_id: str, new_return_datetime: str, email: str,
                       cvv: str, billing_zip: str) -> dict:
    """Extend a rental. Requires the email on file plus CVV and billing ZIP."""
    return _request("POST", f"/reservations/{reservation_id}/extend", body={
        "new_return_datetime": new_return_datetime,
        "email": email,
        "payment": {"use_card_on_file": True, "cvv": cvv, "billing_zip": billing_zip},
    }, idempotent_write=True)


def modify_reservation(reservation_id: str, email: str, cvv: str, billing_zip: str,
                       new_pickup_datetime: str | None = None,
                       new_return_datetime: str | None = None,
                       new_return_location: str | None = None) -> dict:
    """Modify pickup/return time or return location. At least one change required."""
    changes = {k: v for k, v in {
        "new_pickup_datetime": new_pickup_datetime,
        "new_return_datetime": new_return_datetime,
        "new_return_location": new_return_location,
    }.items() if v}
    if not changes:
        raise ValueError("modify_reservation requires at least one change field")
    return _request("POST", f"/reservations/{reservation_id}/modify", body={
        **changes,
        "email": email,
        "payment": {"use_card_on_file": True, "cvv": cvv, "billing_zip": billing_zip},
    }, idempotent_write=True)


def cancel_reservation(reservation_id: str, email: str, reason: str | None = None) -> dict:
    """Cancel a reservation. Returns cancellation_details (penalty, refund_amount)."""
    body: dict[str, Any] = {"email": email}
    if reason:
        body["reason"] = reason
    return _request("POST", f"/reservations/{reservation_id}/cancel", body=body,
                    idempotent_write=True)


if __name__ == "__main__":
    reservation = get_reservation("AVS-29471835")
    print("Connected. Sample reservation:")
    print(f"  {reservation['customer_name']} — {reservation['vehicle']['description']}")
    print(f"  returns {reservation['dates']['current_return_datetime']} at {reservation['return_location']['code']}")
