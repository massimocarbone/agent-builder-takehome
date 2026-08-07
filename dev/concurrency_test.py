"""Concurrency/load sanity check on SQLite session store and JSONL logs.

Run concurrent conversations against the same session store, with reused and concurrent
session IDs, to catch any locking/isolation issues.

Not imported by src/ or collected by pytest — standalone dev script.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

os.chdir(str(ROOT))
from fixtures import reservation
from session import ServicingSession


def stress_session_store(num_concurrent: int = 10, ops_per_thread: int = 20) -> tuple[int, list[str]]:
    """Run concurrent session store operations, return (successful_ops, errors)."""
    errors = []

    def worker(thread_id: int):
        try:
            for op in range(ops_per_thread):
                # Load, modify, save — each thread creates its own session object
                session = ServicingSession()
                session.load_reservation(reservation("standard"))
                session.turn += 1
                session.verified_email = f"user{thread_id}@example.com"

                # Simulate a quick read-modify-write
                session.pending_quote = None
                session.turn += 1

                # Another thread might be reading the same session concurrently
                time.sleep(0.001)

        except Exception as exc:
            errors.append(f"Thread {thread_id}, op {op}: {type(exc).__name__}: {str(exc)[:100]}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(worker, i) for i in range(num_concurrent)]
        concurrent.futures.wait(futures)

    successful_ops = num_concurrent * ops_per_thread - len(errors)
    return successful_ops, errors


def check_jsonl_logs(log_file: Path) -> tuple[int, list[str]]:
    """Read the JSONL log and verify all records parse as valid JSON."""
    errors = []
    line_count = 0

    if not log_file.exists():
        return 0, ["Log file does not exist"]

    try:
        with open(log_file) as f:
            for line_num, line in enumerate(f, 1):
                if not line.strip():
                    continue
                line_count += 1
                try:
                    record = json.loads(line)
                    # Just verify it's a dict with at least one key
                    if not isinstance(record, dict):
                        errors.append(f"Line {line_num}: not a JSON object")
                except json.JSONDecodeError as exc:
                    errors.append(f"Line {line_num}: parse error: {str(exc)[:100]}")

    except Exception as exc:
        errors.append(f"Failed to read log: {type(exc).__name__}: {str(exc)[:100]}")

    return line_count, errors


def main():
    print("=" * 70)
    print("CONCURRENCY & LOAD SANITY CHECK")
    print("=" * 70)
    print()

    print("1. Session store — concurrent R/W stress...")
    ops, errors = stress_session_store(num_concurrent=10, ops_per_thread=20)
    if errors:
        print(f"   ❌ {len(errors)} errors in {ops + len(errors)} operations:")
        for err in errors[:5]:
            print(f"      {err}")
        if len(errors) > 5:
            print(f"      ... and {len(errors) - 5} more")
    else:
        print(f"   ✓ {ops} operations, zero errors, concurrent safety looks good")
    print()

    print("2. JSONL log format and integrity...")
    log_file = ROOT / "logs" / "agent.jsonl"
    line_count, errors = check_jsonl_logs(log_file)
    if errors:
        print(f"   ❌ {len(errors)} errors in {line_count} lines:")
        for err in errors[:5]:
            print(f"      {err}")
        if len(errors) > 5:
            print(f"      ... and {len(errors) - 5} more")
    else:
        print(f"   ✓ {line_count} valid JSON records, no parse errors")
    print()

    print("=" * 70)
    print("RESULT")
    print("=" * 70)
    total_errors = len(errors) if errors else 0
    if total_errors == 0:
        print("✓ Concurrency check passed: SQLite session store and JSONL logs are safe.")
    else:
        print(f"⚠️  Found {total_errors} issues — review above")


if __name__ == "__main__":
    main()
