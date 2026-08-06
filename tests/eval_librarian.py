"""Librarian retrieval-quality eval — scored, non-blocking, real model calls.

Deliberately named eval_* so pytest never collects it: quality is probabilistic and
must not gate a build (tests/test_librarian.py holds the deterministic safety layer).
This is the measurement half of the shadow-mode design — run it to get the numbers
that would justify moving KB_RETRIEVAL_MODE off lexical, or to catch a regression
after a prompt or model change.

Run: python tests/eval_librarian.py          (needs OPENAI_API_KEY in .env)

Scoring per case: hit = every expected id proposed (order ignored); no_coverage cases
require the flag AND zero served ids. Also reported: hallucination and failure rates —
the reliability numbers that decide the mode question, which is why failures score as
misses rather than being skipped (skipping them would bias the eval the same way
success-only logging would bias the shadow log; DECISIONS.md §2).
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # agent.py does this in production; standalone runs need it here
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import librarian  # noqa: E402

# (query, expected ids — every one must be proposed, extras allowed, {} = expect
# no_coverage). The first block mirrors tests/test_kb.py as a regression floor: the
# librarian must not lose queries lexical already gets right. The second block is the
# reason it exists: cases lexical is structurally blind on (homonyms, uncovered topics,
# paraphrase with zero vocabulary overlap).
CASES = [
    # --- regression floor: lexical already gets these right ---
    ("what is the grace period for returns", {"kb_ext_01"}),
    ("how late can I return the car", {"kb_fee_02"}),
    ("cancel my reservation refund", {"kb_can_01"}),
    ("late return fee amount", {"kb_fee_02"}),
    ("do preferred members pay late fees", {"kb_pref_02"}),
    ("return the car early before my return date", {"kb_can_04"}),
    ("extend my rental", {"kb_ext_01"}),
    ("how do I get my money back if I cancel", {"kb_can_02"}),
    ("what happens if I never pick up the vehicle", {"kb_can_03"}),
    ("can someone else extend my rental for me", {"kb_elig_01"}),
    # --- semantic cases: what the librarian is for ---
    ("do you cover insurance or a damage waiver", {}),          # the motivating homonym
    ("what is your pet policy", {}),                            # the motivating meta-term
    ("can I bring my dog in the rental", {}),
    ("is smoking allowed in the car", {}),
    ("do you have child seats", {}),
    ("can I keep the auto a couple more days", {"kb_ext_01"}),  # zero-overlap paraphrase
    ("I want to give the car back sooner than planned", {"kb_can_04"}),
    ("what do I get for being a preferred member", {"kb_upg_03", "kb_pref_01"}),
    ("dropping the car at a different airport", {"kb_fee_03", "kb_mod_02"}),
    ("the car I reserved isn't at the lot", {"kb_mod_04"}),
]


def main() -> int:
    hits, failures, hallucinated_total = 0, 0, 0
    for query, expected in CASES:
        p = librarian.propose(query)
        got = set(p["article_ids"])
        hallucinated_total += len(p["hallucinated_ids"])
        if p["status"] != "ok":
            failures += 1
            verdict, detail = "FAIL", f"status={p['status']} {p['reason'][:60]}"
        elif not expected:
            ok = p["no_coverage"] and not got
            verdict, detail = ("HIT" if ok else "MISS"), \
                f"no_coverage={p['no_coverage']} ids={sorted(got)}"
            hits += ok
        else:
            ok = expected <= got
            verdict, detail = ("HIT" if ok else "MISS"), \
                f"want⊆{sorted(expected)} got={sorted(got)}"
            hits += ok
        print(f"  {verdict:4} {p['latency_ms']:5}ms  {query!r:55} {detail}")

    n = len(CASES)
    print(f"\n{hits}/{n} hit ({hits / n:.0%}) | {failures} call failure(s) | "
          f"{hallucinated_total} hallucinated id(s)")
    return 0 if hits == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
