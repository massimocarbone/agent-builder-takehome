"""LLM-mediated retrieval — a candidate *producer*, not an agent (DECISIONS.md §2).

Picking the best-matching articles from a fixed 30-item list is classification, so this
is a single structured call: index in context, ids out, no loop, no tools, no autonomy.
Every serious bug in this build has come from LLM judgment surface; this module adds as
little of it as the task allows, and everything it proposes still passes through
``kb._finalize`` — the deterministic gate that validates ids against the real corpus,
applies authority suppression, and hydrates bodies verbatim. The librarian can propose
a wrong article; it structurally cannot invent article content, choose between
conflicting authorities, or say anything to the customer.

What it adds over lexical scoring: sense. "Damage waiver" and "late-fee waiver" are one
token apart lexically and a world apart semantically, and the business-context block
lets it answer "that isn't something we publish policy on" — a judgment a similarity
score cannot represent (``no_coverage``, surfaced as a flag; its ``reason`` goes to the
log only, never to the customer, closing "never invent a reason" by construction).

Failure contract: ``propose`` never raises. Any failure — timeout, provider error,
unparseable output, every id hallucinated — becomes a status on the result, and
``retrieve`` falls back to lexical. The lexical retriever isn't replaced by this
module; it is the floor under it. The never-raise rule is load-bearing beyond
politeness: this call runs inside a tool call inside ``run_turn``'s provider-retry
wrapper, and an exception escaping from *here* would trip that outer retry and re-run
tool calls that already succeeded.

Scale note (DECISIONS.md §2): the whole-index-in-context mechanism is right for tens to
low hundreds of articles, and the brief promises unseen *reservations*, never a larger
knowledge base. At genuine scale the producer/finalizer seam is what survives — an
embeddings pass narrows candidates, and this call arbitrates over the short list.
"""
from __future__ import annotations

import json
import time
from functools import lru_cache

import config
import kb
from session import log_event

# What the corpus covers and — the part no similarity score can represent — what it
# doesn't. Grounded in the actual article list, not assumptions about the real Avis.
BUSINESS_CONTEXT = """\
Avis is a car-rental company. This knowledge base covers servicing an EXISTING rental:
extending it, changing pickup/return times or locations, vehicle class changes and
availability, cancelling (including refunds, no-shows, and ending a rental early), late
returns and grace periods, fees (late return, one-way, taxes, out-of-hours returns),
payment/card requirements, who may make changes, and the Avis Preferred membership
program (benefits, eligibility, late-fee waiver).
It publishes NO policy on: insurance products or damage waivers, roadside assistance,
pets or animals in vehicles, smoking, child seats, fuel discounts, tolls or violations,
new bookings or quoted pricing, or anything outside servicing an existing rental.
A question on those topics is no_coverage — do not stretch a near-matching article."""

_PROMPT = """\
You classify one customer question against a fixed list of Avis help-center articles.

Respond with STRICT JSON, nothing else:
{{"article_ids": [...], "no_coverage": true/false, "reason": "..."}}

Rules:
- article_ids: ids of articles that genuinely answer the question, best first, at most
  {max_ids}. Empty list if none do. Use ONLY ids from the list below — never invent one.
- no_coverage: true when the topic is outside what the business context says is covered,
  even if an article shares a word with the question. false if any listed article
  genuinely answers.
- reason: one short sentence explaining the choice, for an internal log. It is never
  shown to the customer.

BUSINESS CONTEXT
{business_context}

ARTICLES (id | authority | category | title | first sentence)
{index}"""

MAX_IDS = 5


@lru_cache(maxsize=1)
def _index() -> str:
    """The compact article index, derived mechanically from the corpus at load.

    Never hand-maintained prose — a second, hand-kept copy of the KB would be a second
    source of truth to drift out of date, the exact failure class the legacy
    grace-period article demonstrates. Rebuilt from the same file kb.py reads.
    """
    lines = []
    for a in kb._corpus():
        first_sentence = a["body"].split(". ")[0].strip()
        lines.append(f"{a['id']} | {a['authority']} | {a['category']} | "
                     f"{a['title']} | {first_sentence}")
    return "\n".join(lines)


def _call_model(query: str) -> str:
    """The one provider call, isolated so tests replace it without touching a network.

    max_retries=0: the OpenAI client retries internally by default, which would spend
    the whole timeout budget before our fallback ever ran. Fallback to lexical IS the
    retry policy here.
    """
    from openai import OpenAI
    client = OpenAI(timeout=config.KB_LIBRARIAN_TIMEOUT_S, max_retries=0)
    response = client.chat.completions.create(
        model=config.KB_LIBRARIAN_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _PROMPT.format(
                max_ids=MAX_IDS, business_context=BUSINESS_CONTEXT, index=_index())},
            {"role": "user", "content": query},
        ],
    )
    return response.choices[0].message.content or ""


def propose(query: str) -> dict:
    """One classification attempt. Never raises; failure is a status, not an exception.

    Returns: status ("ok" | "timeout" | "error" | "hallucinated_all"), article_ids
    (validated against the corpus, capped), hallucinated_ids (proposed but nonexistent —
    a first-class metric, not a silent drop), no_coverage, reason (log-only), latency_ms.
    """
    started = time.monotonic()

    def result(status: str, *, article_ids: list[str] | None = None,
               hallucinated: list[str] | None = None, no_coverage: bool = False,
               reason: str = "") -> dict:
        return {"status": status, "article_ids": article_ids or [],
                "hallucinated_ids": hallucinated or [], "no_coverage": no_coverage,
                "reason": reason,
                "latency_ms": round((time.monotonic() - started) * 1000)}

    try:
        parsed = json.loads(_call_model(query))
        raw_ids = [str(i) for i in parsed.get("article_ids") or []][:MAX_IDS]
        known = kb._article_by_id()
        valid = [i for i in raw_ids if i in known]
        hallucinated = [i for i in raw_ids if i not in known]
        if raw_ids and not valid:
            return result("hallucinated_all", hallucinated=hallucinated,
                          reason=str(parsed.get("reason") or ""))
        return result("ok", article_ids=valid, hallucinated=hallucinated,
                      no_coverage=bool(parsed.get("no_coverage")),
                      reason=str(parsed.get("reason") or ""))
    except Exception as exc:  # noqa: BLE001 - the failure contract IS the feature here
        status = "timeout" if "timeout" in type(exc).__name__.lower() else "error"
        return result(status, reason=f"{type(exc).__name__}: {str(exc)[:200]}")


def retrieve(query: str) -> dict:
    """Mode-dispatched retrieval. Returns {"articles": [...], "no_coverage": bool}.

    lexical   — kb.search only; this module's model call never runs.
    shadow    — both run, lexical is served, the disagreement is logged.
    librarian — librarian serves via kb._finalize; ANY failure (bad status, or every
                proposed id suppressed/unknown) falls back to lexical.

    The comparison event logs every librarian attempt including failures — logging only
    successes would make the "how often does it actually work" number, the one that
    decides whether this ever turns on, look better than it is (DECISIONS.md §2).
    """
    mode = config.KB_RETRIEVAL_MODE
    if mode not in {"shadow", "librarian"}:
        return {"articles": kb.search(query), "no_coverage": False}

    proposal = propose(query)
    lexical_ids = kb.lexical_candidates(query)

    served = "lexical"
    no_coverage = False
    articles: list[dict] | None = None
    if mode == "librarian" and proposal["status"] == "ok":
        if proposal["no_coverage"] and not proposal["article_ids"]:
            served, no_coverage, articles = "librarian", True, []
        else:
            finalized = kb._finalize(query, proposal["article_ids"], source="librarian")
            if finalized:
                served, articles = "librarian", finalized
            # else: the librarian's picks were empty or died in the finalizer
            # (hallucinated, or legacy-suppressed) — the floor takes over.

    lex, lib = set(lexical_ids), set(proposal["article_ids"])
    log_event("kb_retrieval_comparison", mode=mode, query=query,
              librarian_status=proposal["status"],
              lexical_ids=lexical_ids, librarian_ids=proposal["article_ids"],
              hallucinated_ids=proposal["hallucinated_ids"],
              overlap=sorted(lex & lib), librarian_only=sorted(lib - lex),
              lexical_only=sorted(lex - lib), no_coverage=proposal["no_coverage"],
              reason=proposal["reason"], latency_ms=proposal["latency_ms"],
              served=served)

    if articles is None:
        articles = kb.search(query)
    return {"articles": articles, "no_coverage": no_coverage}
