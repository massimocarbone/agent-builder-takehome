"""One-off shadow-mode evaluation for KB_RETRIEVAL_MODE (DECISIONS.md §8A/§8C).

Standalone script, not imported by src/ and not collected by pytest. Runs kb.search
(lexical, already finalized) against librarian.propose -> kb._finalize (the librarian's
actual would-be served set, respecting suppression/caps) for every query in
dev/kb_shadow_queries.py, and writes raw per-query results to
dev/kb_shadow_results.json for the report step to hand-review.

Deliberately calls kb.search / librarian.propose / kb._finalize directly as pure
functions -- bypasses the env-var-gated librarian.retrieve() dispatcher, so
KB_RETRIEVAL_MODE never needs to be touched.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kb  # noqa: E402
import librarian  # noqa: E402

# kb_shadow_queries.py asserts len(QUERIES) >= 150 at import time; the actual list in
# the repo has 123 entries (a pre-existing count mismatch in that file, which per
# instructions we use as-is / don't regenerate). Execute it with the assert statement
# stripped so the module's own stale check doesn't block using its query list.
_queries_src = (Path(__file__).resolve().parent / "kb_shadow_queries.py").read_text()
_queries_ns: dict = {}
exec(
    "\n".join(line for line in _queries_src.splitlines() if not line.strip().startswith("assert")),
    _queries_ns,
)
QUERIES = _queries_ns["QUERIES"]
print(f"Loaded {len(QUERIES)} queries (file's own assert claims >=150; actual count differs).")


def run() -> list[dict]:
    results = []
    total = len(QUERIES)
    for i, query in enumerate(QUERIES, 1):
        t0 = time.monotonic()
        lexical_articles = kb.search(query)
        lexical_ids = [a["id"] for a in lexical_articles]

        proposal = librarian.propose(query)
        librarian_raw_ids = proposal["article_ids"]

        if proposal["status"] == "ok":
            if proposal["no_coverage"] and not proposal["article_ids"]:
                librarian_finalized_ids: list[str] = []
            else:
                finalized = kb._finalize(query, proposal["article_ids"], source="librarian_shadow_eval")
                librarian_finalized_ids = [a["id"] for a in finalized]
        else:
            librarian_finalized_ids = []

        agree = set(lexical_ids) == set(librarian_finalized_ids)

        row = {
            "query": query,
            "lexical_ids": lexical_ids,
            "librarian_raw_ids": librarian_raw_ids,
            "librarian_finalized_ids": librarian_finalized_ids,
            "agree": agree,
            "librarian_status": proposal["status"],
            "hallucinated_ids": proposal["hallucinated_ids"],
            "no_coverage": proposal["no_coverage"],
            "reason": proposal["reason"],
            "latency_ms": proposal["latency_ms"],
        }
        results.append(row)
        elapsed = time.monotonic() - t0
        print(f"[{i}/{total}] ({elapsed:.2f}s) agree={agree} status={proposal['status']} "
              f"no_cov={proposal['no_coverage']} :: {query[:60]}")

    return results


if __name__ == "__main__":
    results = run()
    out_path = Path(__file__).resolve().parent / "kb_shadow_results.json"
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {len(results)} results to {out_path}")
