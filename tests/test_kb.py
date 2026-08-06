"""Regression tests for knowledge-base retrieval. Run: python tests/test_kb.py

These are the assertions the design hinges on — most importantly that the stale legacy
grace-period article can never outrank official policy. Deterministic lexical scoring is
what makes these testable at all; the same query always returns the same articles.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import kb  # noqa: E402


def ids(results):
    return [r["id"] for r in results]


def test_grace_period_trap():
    """THE test. kb_fee_01 (legacy, 2hr) must never appear; kb_ext_01 (official, 30min) must."""
    for query in ["grace period", "what is the grace period for returns",
                  "how late can I return the car", "am I charged if I return late"]:
        got = ids(kb.search(query))
        assert "kb_fee_01" not in got, f"{query!r} surfaced the stale legacy article: {got}"
    got = ids(kb.search("grace period late return"))
    assert "kb_ext_01" in got or "kb_fee_02" in got, f"official policy missing: {got}"


def test_cancellation_policy():
    got = ids(kb.search("cancel my reservation refund"))
    assert got and got[0].startswith("kb_can"), f"expected a cancellation article first: {got}"
    assert "kb_can_01" in got, f"core cancellation policy missing: {got}"


def test_late_fee_amount_comes_from_official_article():
    got = kb.search("late return fee amount")
    assert got and got[0]["authority"] == "official-policy", f"top hit not official: {ids(got)}"
    assert "kb_fee_02" in ids(got), f"the $29 fee article missing: {ids(got)}"


def test_preferred_late_fee_waiver():
    got = ids(kb.search("do preferred members pay late fees"))
    assert "kb_pref_02" in got or "kb_fee_02" in got, f"waiver policy missing: {got}"


def test_early_return_vs_cancel():
    got = ids(kb.search("return the car early before my return date"))
    assert "kb_can_04" in got, f"early-return article missing: {got}"


def test_nonsense_returns_nothing():
    assert kb.search("purple elephant cryptocurrency") == []
    assert kb.search("") == []
    assert kb.search("the and of") == []  # stopwords only


def test_results_carry_provenance():
    for r in kb.search("extend my rental"):
        assert r["id"] and r["authority"] and r["last_updated"], f"missing provenance: {r}"


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
