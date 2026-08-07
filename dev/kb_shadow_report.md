# KB_RETRIEVAL_MODE shadow evaluation (2026-08-06)

Script: `dev/kb_shadow_eval.py`. Raw per-query output: `dev/kb_shadow_results.json`.
Data source: `dev/kb_shadow_queries.py` (`QUERIES`). Live `gpt-4.1-mini` calls via
`librarian.propose`, compared against `kb.search` — both at the *finalized* level
(`kb._finalize`, post-suppression/caps), per the correctness note this pass was
scoped around.

**Note on the query file:** `dev/kb_shadow_queries.py` asserts `len(QUERIES) >= 150`
at import time, but the list it defines has **123** entries, not 150+ — a pre-existing
mismatch in that file. Per instructions the file was used as-is (not regenerated); the
eval script loads its `QUERIES` list directly, bypassing the stale assert, and ran all
123. This discrepancy is worth a one-line fix in that file separately, but is out of
scope for this pass.

## Headline numbers

| Metric | Value |
|---|---|
| Queries run | 123 |
| Agreement (lexical finalized == librarian finalized, as sets) | 12/123 = **9.8%** (disagreement 90.2%) |
| Hallucination rate (any proposed id not in corpus) | 0/123 = **0%** |
| Failure rate (status != "ok") | 1/123 = **0.8%** (one `timeout`) |
| Latency | p50 **1154ms**, p95 **2103ms**, max **4310ms** |

## Disagreement breakdown (all 111 disagreements, categorized)

| Bucket | Count | Pattern |
|---|---|---|
| `subset` — librarian's set ⊂ lexical's set | 43 | Librarian returns fewer, more targeted ids |
| `overlap` — partial overlap, neither subset | 25 | Different rank/mix, shared core article(s) |
| `lib_empty` — librarian empty (mostly `no_coverage=true`), lexical non-empty | 22 | No-coverage judgment calls |
| `disjoint` — no shared ids | 17 | Genuinely different articles picked |
| `lex_empty` — lexical empty, librarian non-empty | 3 | Typos / phrasing lexical's tokenizer misses |
| `fail` — librarian status != ok | 1 | One timeout; production `retrieve()` falls back to lexical here |

**The raw 90.2% disagreement number is misleading on its own** (exactly the trap
DECISIONS.md §2 warns against) — 43 of 111 disagreements (39%) are just
`TOP_N=3` padding: lexical always returns up to 3 scored hits even when only one is
truly relevant, so a case where both agree on the *right* article still counts as a
"disagreement" because lexical tacked on two lower-relevance extras. E.g. "how do I
extend my rental" → lexical `[Extending Your Rental, Who Can Extend or Modify a
Rental, Extensions and Vehicle Availability]`, librarian `[Extending Your Rental]`
alone — same top pick, lexical padded.

## No-coverage precision

Hand-judged genuinely-out-of-scope topics from the query set (pet policy, roadside
assistance, child seat, insurance/damage waiver, smoking, fuel discounts, tolls,
speeding tickets, new bookings, GPS rental, shuttle, additional driver, under-21,
driving into Mexico) — **22 such queries** appear in the set.

| | Correctly flagged as out-of-scope |
|---|---|
| Librarian (`no_coverage=true`, empty finalized set) | **20/22 = 91%** |
| Lexical (empty result) | **0/22 = 0%** — it always returned 1–3 articles, all irrelevant |

The 2 librarian "misses" aren't no-coverage misses at all: "what if I return the car
with less fuel than I got it" and "why was I charged extra for fuel" both correctly
identify the legacy `Fuel and Refueling Charges` (`kb_fee_05`) article as the topical
match — but the finalizer's category-level suppression drops it (its category, `fees`,
has non-legacy coverage from unrelated fee articles) with no fuel-specific replacement
in the corpus. Lexical never surfaces `kb_fee_05` for these queries either and instead
serves *Late Return Fee* / *Preferred Benefits* — confidently wrong. Both retrievers
fail these two queries; the librarian's failure mode is silence, lexical's is a wrong
answer. This is a corpus gap (no non-legacy fuel article exists), not new evidence
against the librarian.

## Hand-review by bucket (sample from each, judged against `data/knowledge-base/articles.json`)

**`disjoint` (17) — sampled 6, librarian right in 5/6:**
- *"I don't need the vehicle anymore, what do I do"* → librarian: `Ending a Rental
  Early` (correct). Lexical: `When a Requested Vehicle Isn't Available`, `Returning
  Late vs. Extending`, `Short Extensions` — none on-topic.
- *"what if nobody shows up to get the car"* → librarian: `No-Show Policy` (correct,
  exact match). Lexical: `Who Can Extend or Modify a Rental`, `How Extension Charges
  Are Calculated` — wrong.
- *"how do refunds work when plans change"* → librarian: `Refund Timing After a
  Cancellation` (correct). Lexical: `One-Way and Location Change Fees`, `Cancelling a
  Reservation`, `Ending a Rental Early` — the cancellation article is adjacent-relevant
  but refund timing is the actually-asked question; librarian's pick is more precise.
- *"I'm not going to make it back in time, what happens"* → librarian: `Late Return
  Fee`, `Returning Late vs. Extending Your Rental` (both on point). Lexical: `No-Show
  Policy` (wrong — this is a late return, not a no-show), `Avis Preferred Benefits`,
  `Changing Your Pickup or Return Time` — a homonym-style miss lexical made because
  "not going to make it back" shares no vocabulary with the right article.
- *"if I extend will my rate change and is there a fee"* → librarian: `How Extension
  Charges Are Calculated`, `Extending Your Rental` (correct, exact topic). Lexical:
  `One-Way and Location Change Fees`, `Who Can Extend or Modify a Rental`, `Late Return
  Fee` — all tangential.
- *"can I extend and also change the pickup location"* (multi-intent, no article
  precisely matches "pickup location" — closest is return-location) → librarian:
  `Extending Your Rental`, `Changing Your Return Location`. Lexical: `One-Way and
  Location Change Fees`, `Changing Your Pickup or Return Time`, `Who Can Extend or
  Modify a Rental`. Neither nails "pickup location" (no such article exists), but
  librarian captures the extend intent that lexical drops entirely — call this a
  narrow librarian edge, not a clean win.

**`lex_empty` (3) — all 3 favor the librarian**, and by a large margin: typo queries
("how do i extned my rentl", "cancelation policy") where lexical's literal-token
matcher returns nothing, and "my situation is complicated, can a human help" where
lexical's stopword/tokenization leaves no scoreable terms. Librarian correctly resolves
all three semantically (`Extending Your Rental`; `Cancelling a Reservation` + `Refund
Timing` + `No-Show Policy`; `When We Connect You to a Representative`).

**`lib_empty` (22) — 20/22 genuine no-coverage wins for the librarian** (see table
above); the 2 fuel-related misses are a shared corpus gap, not a librarian-specific
regression (both retrievers fail them, in different ways — see above).

**`subset` (43) — librarian narrower and, on every sample checked, correct.** Spot
checks ("how do I extend my rental", "can I change my pickup time", "how do I cancel
my reservation", "how do I upgrade to Avis Preferred", "who is allowed to extend or
modify a rental", "are there fees for returning outside business hours") all show the
librarian's single pick matching lexical's top-ranked pick exactly, with lexical's
2nd/3rd slots being lower-relevance padding from `TOP_N=3`. Not a case of the librarian
missing coverage lexical has — a case of lexical over-serving.

**`overlap` (25) — mixed, mostly favors the librarian's ranking/composition.** E.g.
*"what are the Avis Preferred membership benefits"* → librarian:
`[Avis Preferred Benefits, Avis Preferred Program Overview, Preferred Member Late-Fee
Waiver]`; lexical: `[Avis Preferred Benefits, Upgrading to Avis Preferred, Avis
Preferred Program Overview]` — lexical's 2nd pick ("Upgrading to...") answers "how do I
upgrade," not "what are the benefits"; librarian's set stays on-topic throughout.

**The one legacy-suppression case worth calling out explicitly:** *"what is the grace
period for late returns"* — the librarian's raw proposal was `[kb_fee_01]` (the stale
legacy grace-period article — the exact known trap this whole feature exists to catch,
DECISIONS.md §2), and `_finalize` suppressed it, leaving the librarian's served set
empty. Lexical, meanwhile, returned `[Returning Late vs. Extending Your Rental, Late
Return Fee, Preferred Member Late-Fee Waiver]` — correct articles, because its
score-floor+TOP_N padding pulled in the right answer alongside (never actually scored
`kb_fee_01` above threshold for this exact phrasing). This is the safety mechanism
firing correctly on the librarian side (a wrong-authority article never reached the
customer) but it costs a served answer entirely rather than falling through to a
different, correct proposal — a real quality gap in `propose()`'s article selection
(it should have preferred `kb_fee_02`/`kb_ext_05` for this framing), not a flaw in
`_finalize`.

## Recommendation

**Evidence leans toward turning `librarian` on, but short of a full flip in this
pass — recommend `shadow` next, not `librarian` by default yet.**

Against the stated bar ("stay off unless disagreement rate is >90% AND majority of
disagreement samples favor the librarian"): disagreement is 90.2% (just over the
line) and the hand review does show a clear majority of the *substantive*
disagreements favoring the librarian — 20/22 correct no-coverage calls where lexical
was silently wrong every time, 3/3 typo/paraphrase wins, and 5/6 to 6/6 wins across
the `disjoint`/`overlap` samples. Zero hallucinations across 123 live calls is also
a strong signal.

What holds this back from an outright default flip:
- **~1.1–2.1s added latency per KB lookup** (p50/p95) for every policy question, on
  top of the existing turn latency, for a corpus this small (30 articles) where
  lexical's problems are narrow and already mostly closed (homonym suppression via
  `_finalize` already protects lexical mode too).
- **One failure in 123 calls (0.8%)** is fine in isolation, but shadow mode gets that
  measured at higher volume, across real sessions with real tool-call retry
  interactions, before it's the default path customers depend on.
- The one clear miss found (`kb_fee_01` grace-period case) shows `propose()`'s
  selection quality, not just the finalizer's suppression, still has room to
  improve — worth another look before this is the primary producer.

Net: this data justifies moving `KB_RETRIEVAL_MODE` from `lexical` to **`shadow`** now
(collect real-traffic comparison data risk-free, served answers stay lexical), with a
follow-up decision to flip to `librarian` once shadow data at volume confirms these
123-query findings and the `propose()` selection gap above is addressed. Not
recommending a direct flip to `librarian` as this pass's outcome.
