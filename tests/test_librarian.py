"""Librarian wiring tests — deterministic, blocking, no model in the loop.

Everything here runs against a stubbed `_call_model`; nothing touches a network. The
split is deliberate (DECISIONS.md §2): safety properties — fallback, suppression,
verbatim content, failure isolation — are asserted here and gate the build. Retrieval
*quality* is probabilistic and lives in tests/eval_librarian.py as a scored eval, not a
pass/fail gate; a green suite must not depend on a model's mood.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent  # noqa: E402
import config  # noqa: E402
import kb  # noqa: E402
import librarian  # noqa: E402


def _stub(payload=None, exc: Exception | None = None):
    """Swap librarian._call_model; returns (restore, calls) so tests can assert reach."""
    calls: list[str] = []

    def fake(query: str) -> str:
        calls.append(query)
        if exc:
            raise exc
        return json.dumps(payload)

    orig = librarian._call_model
    librarian._call_model = fake
    return (lambda: setattr(librarian, "_call_model", orig)), calls


def _mode(mode: str):
    orig = config.KB_RETRIEVAL_MODE
    config.KB_RETRIEVAL_MODE = mode
    return lambda: setattr(config, "KB_RETRIEVAL_MODE", orig)


# --- _finalize: the deterministic gate, fed as if by a hostile producer --------------

def test_finalize_drops_hallucinated_ids_and_counts_them():
    out = kb._finalize("late fee", ["kb_fake_99", "kb_fee_02", "kb_nope_1"], source="librarian")
    assert [r["id"] for r in out] == ["kb_fee_02"], out


def test_finalize_suppresses_legacy_regardless_of_producer():
    """A producer proposing ONLY the stale article gets nothing served — the librarian
    equivalent of the solo-lexical-match gap, closed by the same absolute rule."""
    for legacy_id in ["kb_fee_01", "kb_fee_05"]:
        out = kb._finalize("grace period", [legacy_id], source="librarian")
        assert out == [], f"producer-proposed legacy {legacy_id} was served: {out}"


def test_finalize_output_is_verbatim_corpus_content():
    """The named guarantee: producers propose ids, never text. Every title and body in
    the output must be byte-identical to the corpus entry for that id."""
    corpus = {a["id"]: a for a in kb._corpus()}
    out = kb._finalize("cancel refund", ["kb_can_01", "kb_can_02"], source="librarian")
    assert out, "expected results to compare"
    for r in out:
        assert r["title"] == corpus[r["id"]]["title"]
        assert r["body"] == corpus[r["id"]]["body"]
        assert r["authority"] == corpus[r["id"]]["authority"]


def test_finalize_caps_and_dedupes():
    out = kb._finalize("fees", ["kb_fee_02", "kb_fee_02", "kb_fee_03", "kb_fee_04",
                                "kb_can_01", "kb_can_02"], source="librarian")
    got = [r["id"] for r in out]
    assert len(got) == kb.TOP_N and len(set(got)) == len(got), got


# --- propose: the failure contract ---------------------------------------------------

def test_propose_ok_path_validates_ids():
    restore, _ = _stub({"article_ids": ["kb_fee_02", "kb_bogus_7"],
                        "no_coverage": False, "reason": "late fee question"})
    try:
        p = librarian.propose("late fee")
    finally:
        restore()
    assert p["status"] == "ok"
    assert p["article_ids"] == ["kb_fee_02"]
    assert p["hallucinated_ids"] == ["kb_bogus_7"]


def test_propose_all_hallucinated_is_a_failure_status():
    restore, _ = _stub({"article_ids": ["kb_x_1", "kb_x_2"], "no_coverage": False})
    try:
        p = librarian.propose("anything")
    finally:
        restore()
    assert p["status"] == "hallucinated_all" and p["article_ids"] == []


def test_propose_never_raises():
    class FakeAPITimeoutError(Exception):
        pass

    for exc, expected in [(FakeAPITimeoutError("slow"), "timeout"),
                          (ValueError("boom"), "error"),
                          (RuntimeError("provider down"), "error")]:
        restore, _ = _stub(exc=exc)
        try:
            p = librarian.propose("q")
        finally:
            restore()
        assert p["status"] == expected, (type(exc).__name__, p)

    restore, _ = _stub(payload=None)  # json.dumps(None) -> "null": unparseable shape
    try:
        p = librarian.propose("q")
    finally:
        restore()
    assert p["status"] == "error", p


# --- retrieve: mode dispatch and the lexical floor -----------------------------------

def test_lexical_mode_never_invokes_the_model():
    restore_stub, calls = _stub({"article_ids": ["kb_fee_02"]})
    restore_mode = _mode("lexical")
    try:
        out = librarian.retrieve("grace period late return")
    finally:
        restore_mode(); restore_stub()
    assert calls == [], "lexical mode reached the model"
    assert out["articles"] == kb.search("grace period late return")


def test_shadow_mode_serves_lexical_even_when_librarian_disagrees():
    restore_stub, calls = _stub({"article_ids": ["kb_upg_01"], "no_coverage": False})
    restore_mode = _mode("shadow")
    try:
        out = librarian.retrieve("grace period late return")
    finally:
        restore_mode(); restore_stub()
    assert len(calls) == 1, "shadow mode never reached the model — proves nothing"
    assert [a["id"] for a in out["articles"]] == \
        [a["id"] for a in kb.search("grace period late return")]
    assert "kb_upg_01" not in [a["id"] for a in out["articles"]]


def test_librarian_mode_serves_finalized_picks():
    restore_stub, _ = _stub({"article_ids": ["kb_pref_02"], "no_coverage": False,
                             "reason": "late-fee waiver"})
    restore_mode = _mode("librarian")
    try:
        out = librarian.retrieve("do preferred members pay late fees")
    finally:
        restore_mode(); restore_stub()
    assert [a["id"] for a in out["articles"]] == ["kb_pref_02"], out


def test_librarian_failure_falls_back_to_lexical():
    for stub_kwargs in [dict(exc=TimeoutError("slow")),
                        dict(payload={"article_ids": ["kb_fake_1"]}),   # hallucinated_all
                        dict(payload={"article_ids": ["kb_fee_01"]}),   # dies in finalize
                        dict(payload={"article_ids": [], "no_coverage": False})]:  # empty
        restore_stub, _ = _stub(**stub_kwargs)
        restore_mode = _mode("librarian")
        try:
            out = librarian.retrieve("grace period late return")
        finally:
            restore_mode(); restore_stub()
        expected = [a["id"] for a in kb.search("grace period late return")]
        assert [a["id"] for a in out["articles"]] == expected, (stub_kwargs, out)


def test_no_coverage_is_served_as_empty_with_the_flag():
    restore_stub, _ = _stub({"article_ids": [], "no_coverage": True,
                             "reason": "Avis does not sell insurance"})
    restore_mode = _mode("librarian")
    try:
        out = librarian.retrieve("do you cover insurance or a damage waiver")
    finally:
        restore_mode(); restore_stub()
    assert out["articles"] == [] and out["no_coverage"] is True, out


# --- the comparison log: every attempt, including failures ---------------------------

def test_failed_attempts_are_logged_not_just_successes():
    """Success-only logging would bias the exact dataset that decides whether the
    librarian ever turns on (DECISIONS.md §2)."""
    events = []
    orig_log = librarian.log_event
    librarian.log_event = lambda event, **f: events.append((event, f))
    restore_stub, _ = _stub(exc=TimeoutError("slow"))
    restore_mode = _mode("shadow")
    try:
        librarian.retrieve("grace period")
    finally:
        restore_mode(); restore_stub()
        librarian.log_event = orig_log
    comparisons = [f for e, f in events if e == "kb_retrieval_comparison"]
    assert comparisons, "a failed librarian attempt produced no comparison log"
    assert comparisons[0]["librarian_status"] == "timeout", comparisons[0]
    assert comparisons[0]["served"] == "lexical"


# --- nested-failure isolation: nothing from here may reach run_turn's retry ----------

def test_librarian_provider_failure_never_escapes_the_tool():
    """The librarian call is nested inside a tool inside run_turn's provider-retry
    wrapper. If a nested rate-limit/timeout escaped as an exception, the outer retry
    would re-run the whole turn — including tool calls that already succeeded. A
    provider-shaped failure here must come back as a served result, never a raise."""
    class RateLimitError(Exception):  # same name run_turn's wrapper matches on
        pass

    restore_stub, _ = _stub(exc=RateLimitError("429"))
    restore_mode = _mode("librarian")
    try:
        result = agent.search_policy.__wrapped__("grace period late return")
    finally:
        restore_mode(); restore_stub()
    assert result["ok"] is True, result
    assert [a["id"] for a in result["articles"]] == \
        [a["id"] for a in kb.search("grace period late return")]


def test_search_policy_surfaces_no_coverage_distinctly():
    restore_stub, _ = _stub({"article_ids": [], "no_coverage": True, "reason": "x"})
    restore_mode = _mode("librarian")
    try:
        result = agent.search_policy.__wrapped__("what is your pet policy")
    finally:
        restore_mode(); restore_stub()
    assert result["articles"] == []
    assert "outside what Avis publishes" in result["note"], result


if __name__ == "__main__":
    raise SystemExit("Run with: python -m pytest tests/test_librarian.py")
