"""Knowledge-base retrieval — Layer 3 (DECISIONS.md §2).

Lexical scoring with authority-aware ranking, deliberately not embeddings: at 30
articles, recall is not the failure mode. Retrieving the *wrong-authority* article is —
the corpus contains a stale `legacy` page whose grace period is wrong by 90 minutes.
Deterministic scoring also makes that trap a regression test (see tests/test_kb.py).

Rules enforced here rather than left to the model:
- Authority weighting: official-policy > help-center > legacy.
- Conflict suppression: a `legacy` article is dropped whenever a higher-authority
  article in the same category also matched — the model never sees the conflict.
- Score floor: below it, return nothing; the agent says it has no policy on the
  question rather than improvising.
- Provenance: every query logs the article ids, authority, and dates it returned.
  This is both the debugging trail for a wrong answer and, aggregated, a report of
  which help-center pages have gone stale (complaints cluster on an article id).

The KB answers policy questions in prose. It never supplies a number that drives
money — deterministic policy functions do that, citing an article, not quoting it.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from session import log_event

ARTICLES_PATH = Path(__file__).resolve().parent.parent / "data" / "knowledge-base" / "articles.json"

AUTHORITY_WEIGHT = {"official-policy": 1.0, "help-center": 0.7, "legacy": 0.25}

# Below this post-weighting score a match is noise, not an answer.
SCORE_FLOOR = 1.0
TOP_N = 3

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for", "from",
    "how", "i", "if", "in", "is", "it", "my", "of", "on", "or", "the", "to", "was",
    "what", "when", "where", "will", "with", "you", "your",
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOPWORDS]


@lru_cache(maxsize=1)
def _corpus() -> list[dict]:
    articles = json.loads(ARTICLES_PATH.read_text())
    for a in articles:
        a["_title_tokens"] = set(_tokens(a["title"]))
        a["_category_tokens"] = set(_tokens(a["category"].replace("-", " ")))
        a["_body_tokens"] = set(_tokens(a["body"]))
    return articles


@lru_cache(maxsize=1)
def _idf() -> dict[str, float]:
    """Inverse document frequency: rare terms discriminate, ubiquitous ones don't.

    Without this, a token like "return" — present in nearly every article — swamps the
    one token that actually identifies the topic (e.g. "early" in an early-return
    question). log(N/df) is the textbook damping and keeps scoring deterministic.
    """
    from math import log
    articles = _corpus()
    n = len(articles)
    df: dict[str, int] = {}
    for a in articles:
        for tok in a["_title_tokens"] | a["_category_tokens"] | a["_body_tokens"]:
            df[tok] = df.get(tok, 0) + 1
    return {tok: log(n / count) for tok, count in df.items()}


def _score(query_tokens: list[str], article: dict) -> float:
    """IDF-damped term overlap, weighted toward title and category, then by authority."""
    idf = _idf()
    raw = 0.0
    for tok in query_tokens:
        weight = idf.get(tok, 0.0)
        if tok in article["_title_tokens"]:
            raw += 3.0 * weight
        elif tok in article["_category_tokens"]:
            raw += 2.0 * weight
        elif tok in article["_body_tokens"]:
            raw += 1.0 * weight
    return raw * AUTHORITY_WEIGHT.get(article["authority"], 0.5)


def search(query: str) -> list[dict]:
    """Return up to TOP_N relevant articles, best first, with provenance logged.

    Empty list means "no policy found" — the caller must say so, not improvise.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return []

    scored = [(s, a) for a in _corpus() if (s := _score(query_tokens, a)) >= SCORE_FLOOR]
    # Sort: score desc, then authority desc, then recency desc.
    scored.sort(key=lambda p: (p[0], AUTHORITY_WEIGHT.get(p[1]["authority"], 0.5),
                               p[1]["last_updated"]), reverse=True)

    # Conflict suppression: drop any legacy article whose category is also covered by a
    # higher-authority match. The stale grace-period article dies here, every time.
    categories_covered = {a["category"] for _, a in scored if a["authority"] != "legacy"}
    results = [(s, a) for s, a in scored
               if not (a["authority"] == "legacy" and a["category"] in categories_covered)]

    top = results[:TOP_N]
    log_event("kb_retrieval", query=query,
              returned=[{"id": a["id"], "authority": a["authority"],
                         "last_updated": a["last_updated"], "score": round(s, 2)}
                        for s, a in top],
              suppressed=[a["id"] for s, a in scored
                          if a["authority"] == "legacy" and a["category"] in categories_covered])

    return [{"id": a["id"], "title": a["title"], "authority": a["authority"],
             "last_updated": a["last_updated"], "body": a["body"]}
            for _, a in top]
