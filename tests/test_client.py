"""Client-layer tests: retries, idempotency, classification, payload validation.

These run the REAL avis_client code against a scripted fake transport — no network, no
LLM. Run: python tests/test_client.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import avis_client  # noqa: E402
from avis_client import AvisAPIError  # noqa: E402
from fixtures import (  # noqa: E402
    ConnectionError_, FakeResponse, Timeout, error_body, fake_transport, reservation,
)

avis_client.BACKOFF_BASE_S = 0  # no real sleeping in tests

OK_WRITE = FakeResponse(200, {"success": True, "confirmation_number": "EXT-TEST-1"})


def test_transient_5xx_is_retried_until_success():
    with fake_transport([FakeResponse(503, error_body("SERVICE_UNAVAILABLE")),
                         Timeout,
                         FakeResponse(200, reservation())]) as calls:
        result = avis_client.get_reservation("AVS-48372915")
    assert len(calls) == 3, f"expected 3 attempts, saw {len(calls)}"
    assert result["reservation_id"] == "AVS-48372915"


def test_terminal_4xx_is_never_retried():
    with fake_transport([FakeResponse(404, error_body("RESERVATION_NOT_FOUND"))]) as calls:
        try:
            avis_client.get_reservation("AVS-00000000")
            raise AssertionError("expected AvisAPIError")
        except AvisAPIError as e:
            assert e.code == "RESERVATION_NOT_FOUND" and not e.retryable
    assert len(calls) == 1, f"a 404 was retried: {len(calls)} attempts"


def test_exhausted_retries_classified_and_bounded():
    script = [FakeResponse(503, error_body("SERVICE_UNAVAILABLE"))] * avis_client.MAX_ATTEMPTS
    with fake_transport(script) as calls:
        try:
            avis_client.get_reservation("AVS-48372915")
            raise AssertionError("expected AvisAPIError")
        except AvisAPIError as e:
            assert e.code == "EXHAUSTED_RETRIES" and e.retryable
    assert len(calls) == avis_client.MAX_ATTEMPTS


def test_idempotency_key_stable_across_retries():
    """THE seam where retries become double-charges. The key must not change between
    attempts of one logical write — if it did, the replay guarantee is void."""
    with fake_transport([Timeout, ConnectionError_, OK_WRITE]) as calls:
        avis_client.cancel_reservation("AVS-48372915", "marcus.lee@example.com")
    keys = {c["headers"].get("Idempotency-Key") for c in calls}
    assert len(calls) == 3
    assert len(keys) == 1 and None not in keys, f"key changed across retries: {keys}"


def test_fresh_write_gets_fresh_idempotency_key():
    with fake_transport([OK_WRITE]) as first:
        avis_client.cancel_reservation("AVS-48372915", "marcus.lee@example.com")
    with fake_transport([OK_WRITE]) as second:
        avis_client.cancel_reservation("AVS-48372915", "marcus.lee@example.com")
    k1 = first[0]["headers"]["Idempotency-Key"]
    k2 = second[0]["headers"]["Idempotency-Key"]
    assert k1 != k2, "distinct logical writes must not share an idempotency key"


def test_reads_do_not_send_idempotency_key():
    with fake_transport([FakeResponse(200, reservation())]) as calls:
        avis_client.get_reservation("AVS-48372915")
    assert "Idempotency-Key" not in calls[0]["headers"]


def test_lost_response_retry_shape():
    """A write succeeds server-side but the response is lost to a timeout; the retry
    carries the same key, so the server replays rather than double-acts. We can't see
    the server's dedupe, but we CAN prove our side holds up its half of the contract."""
    with fake_transport([Timeout, OK_WRITE]) as calls:
        result = avis_client.extend_reservation(
            "AVS-48372915", "2026-06-12T15:00:00-05:00",
            "marcus.lee@example.com", "123", "60601")
    assert result["confirmation_number"] == "EXT-TEST-1"
    assert calls[0]["headers"]["Idempotency-Key"] == calls[1]["headers"]["Idempotency-Key"]


def test_malformed_reservation_rejected_at_boundary():
    for dotted in ["dates", "pricing", "status", "payment"]:
        from fixtures import DELETE
        broken = reservation(**{dotted: DELETE})
        with fake_transport([FakeResponse(200, broken)]):
            try:
                avis_client.get_reservation("AVS-48372915")
                raise AssertionError(f"missing {dotted!r} was accepted")
            except AvisAPIError as e:
                assert e.code == "MALFORMED_RESERVATION", f"{dotted}: got {e.code}"
                assert e.details["missing_fields"], dotted


def test_null_datetime_rejected_at_boundary():
    broken = reservation(**{"dates.current_return_datetime": None})
    with fake_transport([FakeResponse(200, broken)]):
        try:
            avis_client.get_reservation("AVS-48372915")
            raise AssertionError("null datetime was accepted")
        except AvisAPIError as e:
            assert e.code == "MALFORMED_RESERVATION"
            assert "dates.current_return_datetime" in e.details["missing_fields"]


def test_secrets_never_reach_the_log(tmp_check=None):
    import json as j
    with fake_transport([OK_WRITE]):
        avis_client.extend_reservation("AVS-48372915", "2026-06-12T15:00:00-05:00",
                                       "marcus.lee@example.com", "987", "60601")
    last = j.loads(open(avis_client.LOG_DIR / "api.jsonl").readlines()[-1])
    flat = j.dumps(last)
    assert "987" not in flat and "marcus.lee" not in flat, f"secret leaked: {flat}"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted({k: v for k, v in globals().items() if k.startswith("test_")}.items()):
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)
