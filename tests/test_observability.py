"""Focused checks for correlation, redaction, and artifact safety."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import avis_client
from fixtures import FakeResponse, Timeout, fake_transport, reservation
from observability import correlation_context, current_correlation, redact


RUNNER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "run_tests.py"
spec = importlib.util.spec_from_file_location("local_test_runner", RUNNER_PATH)
runner = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runner)


def test_correlation_context_is_nested_and_restored():
    before = current_correlation()
    with correlation_context(run_id="run-1", conversation_id="conversation-1"):
        assert current_correlation()["run_id"] == "run-1"
        with correlation_context(operation_id="operation-1"):
            assert current_correlation()["operation_id"] == "operation-1"
        assert "operation_id" not in current_correlation()
    assert current_correlation() == before


def test_recursive_redaction_covers_keys_sequences_and_free_text():
    payload = {
        "email": "person@example.com",
        "nested": [{"payment": {"cvv": "847", "billing_zip": "02110"}}],
        "message": "email person@example.com, cvv is 847, card 4111 1111 1111 1111",
    }
    flat = json.dumps(redact(payload))
    for secret in ("person@example.com", "847", "02110", "4111"):
        assert secret not in flat


def test_api_attempts_share_one_operation_id_and_inherit_outer_context(tmp_path):
    captured = []
    original = avis_client._log
    avis_client._log = lambda **fields: captured.append({**current_correlation(), **fields})
    try:
        with correlation_context(run_id="run-2", test_id="test-retry",
                                 conversation_id="conversation-2", turn_id=3):
            with fake_transport([Timeout, FakeResponse(200, reservation())]):
                avis_client.get_reservation("AVS-48372915")
    finally:
        avis_client._log = original

    assert [event["attempt"] for event in captured] == [1, 2]
    assert len({event["operation_id"] for event in captured}) == 1
    assert all(event["run_id"] == "run-2" for event in captured)
    assert all(event["conversation_id"] == "conversation-2" for event in captured)
    assert all(event["turn_id"] == 3 for event in captured)


def test_artifact_secret_scan_reports_locations_without_echoing_values(tmp_path):
    clean = tmp_path / "agent.jsonl"
    dirty = tmp_path / "api.jsonl"
    clean.write_text('{"email":"***"}\n')
    dirty.write_text(
        '{"message":"contact person@example.com"}\n'
        '{"message":"card 4111 1111 1111 1111"}\n'
        '{"message":"cvv is 847"}\n'
    )
    findings = runner.scan_for_secrets([clean, dirty])
    assert findings == [
        {"file": "api.jsonl", "line": 1},
        {"file": "api.jsonl", "line": 2},
        {"file": "api.jsonl", "line": 3},
    ]
    assert "person@example.com" not in json.dumps(findings)


def test_artifact_secret_scan_does_not_treat_long_float_as_card(tmp_path):
    money_log = tmp_path / "agent.jsonl"
    money_log.write_text('{"actual_penalty":39.980000000000004}\n')
    assert runner.scan_for_secrets([money_log]) == []


def test_artifact_secret_scan_does_not_treat_correlation_uuid_as_card(tmp_path):
    api_log = tmp_path / "api.jsonl"
    api_log.write_text('{"operation_id":"47869973-3928-4618-9234-123456789012"}\n')
    assert runner.scan_for_secrets([api_log]) == []


def test_artifact_directory_rejects_path_traversal(tmp_path):
    for unsafe in ("../escape", "/absolute", "a/b"):
        try:
            runner.create_artifact_dir(tmp_path, unsafe)
            raise AssertionError(f"unsafe run id accepted: {unsafe}")
        except ValueError:
            pass
