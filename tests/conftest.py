"""Hermetic test defaults, correlation context, and artifact summaries."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Production imports remain fail-fast on missing credentials. Tests replace HTTP with a
# scripted transport and therefore use explicitly fake values in a clean checkout.
os.environ.setdefault("AVIS_API_URL", "https://local.test.invalid")
os.environ.setdefault("AVIS_API_KEY", "local-test-key")

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from observability import set_correlation  # noqa: E402


def pytest_configure(config):
    set_correlation(run_id=os.environ.get("TEST_RUN_ID", "pytest-direct"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Keep the test id bound across setup, call, and fixture teardown."""
    from observability import correlation_context

    with correlation_context(test_id=item.nodeid):
        yield


def pytest_sessionfinish(session, exitstatus):
    artifact_dir = os.environ.get("TEST_ARTIFACT_DIR")
    if not artifact_dir:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = reporter.stats if reporter is not None else {}
    counts = {name: len(stats.get(name, [])) for name in (
        "passed", "failed", "error", "skipped", "xfailed", "xpassed",
    )}
    summary = {
        "exit_code": int(exitstatus),
        "counts": counts,
        "collected": session.testscollected,
    }
    path = Path(artifact_dir) / "summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
