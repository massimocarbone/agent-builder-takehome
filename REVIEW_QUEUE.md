# Review queue

Small issues found in passing, left for a human pass rather than fixed inline. Each item
names the file, the problem, and the decision to be made — not a patch to rubber-stamp.

Cleared items move to the bottom with their resolution.

## Open

> Numbers are stable — cleared items keep their id and move to the bottom rather than
> being renumbered, so commit messages and PR comments stay resolvable.
>
> Items 2–9 came from one unscripted human session on 2026-08-05, and 12–13 from a
> second. Two real conversations surfaced more than every scripted test combined —
> worth repeating before each merge.

### 1. Agent volunteers the remaining verification-attempt count
**Where:** `src/extend_flow.py`, `commit_extension`, the `VERIFICATION_FAILED` branch.

The customer-facing message says `Attempt N of 3`, and the model relays it ("you still have
two more tries"). Friendlier, but it also tells someone probing a reservation ID exactly how
much runway they have before the session escalates.

**The call:** keep it (transparency, and the reservation ID is the weaker secret anyway) or
drop the count and let escalation arrive unannounced. Either is defensible; it's a
security-vs-UX judgment, which is why it's here rather than silently decided.

---

### 4. No frustration / no-progress escalation trigger
The session degenerated into `no` → `no` → `what` → `no i wann wtd`, with the agent
returning cheerful non-answers each time. The tester's own note: *"at this point we
should have likely had a real person step in."* Correct.

Every current escalation trigger is **event-based** (verification failures, API errors,
explicit out-of-scope). Nothing watches the *conversation* for degradation: repeated
short/negative turns, no state change across N turns, profanity, or explicit
frustration. This is a cheap, high-value addition and it belongs in code next to the
other triggers, not in the prompt.

### 7. Customer name disclosed before verification
The agent signed off "Have a great day, Marcus" with no email verification. Reads are open
API-side, so anyone holding a reservation ID can obtain the name — but the agent
volunteering it makes a reservation ID a slightly better probe than it needs to be.
Consider withholding the name until verification, or accepting it as the API's posture.

### 8. Weak retrieval on one long query
`"late fee policy for rentals extended past original return"` returned **kb_can_03
(No-Show Policy)** as the top hit. IDF weighting on a long query pulled a wrong document.
Worth adding as a regression test once tuned.

### 9. Malformed CVV accepted, then disclosed
The tester entered a 4-digit CVV; the API accepted it and the extension succeeded. Two
issues: no client-side format validation, and the agent then *told the customer* the
system had accepted an invalid value — an unnecessary disclosure about payment-validation
weakness.

### 10. `SCORE_FLOOR` is documented as a safety boundary; it isn't one
**Where:** `src/kb.py` — the module docstring and the comment above `SCORE_FLOOR`.

Both claim the floor is where "the agent says it has no policy rather than improvising."
Measured, that is false: **in-domain and out-of-domain score ranges overlap**, so no
threshold separates them.

| | query | top score |
|---|---|---|
| lowest legitimate | "what is the grace period" | **2.81** |
| highest false positive | "do you cover insurance or a damage waiver" | **8.12** |

The false positive wins because "waiver" is rare, so IDF weights it heavily, and it sits
in the *title* of `kb_pref_02` ("Preferred Member Late-Fee Waiver"). Semantically
unrelated, lexically a strong match — and IDF makes this class of failure worse, not
better, since rare-word collisions score highest. Same shape: "is there a pet **policy**"
→ "No-Show **Policy**" at 6.04. Requiring a title/category match doesn't help; both false
positives match on title.

`1.0` is a fine value — in-domain top hits run 2.81–12.49, so anything below ~2.5 never
blocks a real answer, and what it actually prunes is the incidental tail (e.g. a
third-place hit at 0.92). **The fix is the wording, not the number.** Restate the floor as
noise-pruning within a result set, and document that the real knowledge boundary is the
model's relevance judgment over article titles — which is the right place for it, since
"does this article address the question" is a language judgment, not a money decision.

Verified in practice: asked about insurance and pet policy, the agent declined both and
said it had no article on pets. The mechanism works; only the comment is wrong.

### 11. `test_nonsense_returns_nothing` gives false confidence
**Where:** `tests/test_kb.py`.

"purple elephant cryptocurrency" scores 0 because none of those words appear anywhere in
the corpus — the test passes with *any* floor, including 0.0001. It exercises nothing.

Replace with the plausible-but-uncovered queries that actually probe the boundary:
insurance / damage waiver, pet policy, roadside assistance, child seat, holiday hours.
Because retrieval *will* return something for these (see item 10), the meaningful
assertion is at the **agent** level — that it declines — not at the retrieval level.

### 17. The agent has no authoritative source for "today" — high severity
**Where:** whole codebase. `datetime.now()` appears in exactly one place
(`policy.py`, Cancel's 48h-window math) and is never surfaced to the model.

Proven, not suspected: a completely fresh conversation with zero prior context, asked
only "what is today's date?", answered **"June 21, 2024."** Real system time at the
moment of the test: **August 5, 2026** — over two years off, in the wrong direction, a
pure hallucination near the model's training cutoff.

**A live human session (2026-08-05, pre-Cancel) shows the failure mode this produces in
practice.** The customer told the bot "it's currently August 3" mid-conversation; asked
directly afterward, the bot answered "August 3, 2026" — almost certainly parroting the
customer's own unverified claim back as fact, not computing it. The same session nearly
let a customer "extend" a reservation to a date that was still in the past relative to
real time, because `extend_flow.build_quote` only validates the target against the
*reservation's own stored return time*, never against real wall-clock time. It happened
to get caught because the customer noticed and corrected themselves — the code did not
catch it.

Tested whether relative phrasing ("extend it by one more week from today") inherits the
bad date: in that instance the model anchored on the reservation's own stored return
date instead of its hallucinated "today," producing a correct result — but that's luck,
not a guarantee. Nothing stops the model from reasoning off its invented June 2024 in a
different phrasing, and Cancel's `policy.py` proves the fix is already known: it uses
real `datetime.now()` internally for exactly this reason. Extend just never got the
equivalent.

**Fix:** (1) inject real `datetime.now()` into the agent's context so "what day is it"
and relative-date reasoning ground out in a fact rather than a guess, and (2) add a
floor to `extend_flow.build_quote` rejecting a target still before real now — the same
rigor `policy.py` already has, just not yet shared. Not merely a demo-data cosmetic
issue: this is "today" being effectively customer-suppliable, which is backwards for
any date-sensitive business logic.

### 18. A compound confirmation question can produce ambiguous consent
**Where:** `src/agent.py` INSTRUCTIONS (cancel flow), observed live 2026-08-05
(`AVS-99004050`).

The customer asked to cancel; the next message was an unrelated garbled aside that
looked enough like a reservation ID to trigger a failed lookup ("did you mean the
original?"); the customer's reply — **"yeah do the original"** — was treated as full
consent to cancel a real reservation. Structurally sound (the turn-boundary and
staged-estimate gates were both satisfied), but the bot's own question had bundled two
things into one ask — "confirm the reservation ID" and "confirm you want to cancel" —
and "yeah do the original" answers the first far more clearly than the second. The
customer's very next message, unprompted, was **"did i confirm cancel?"** — a live
signal that even they weren't certain.

This is the boundary our code-level gates cannot close on their own: they guarantee a
turn passed and *some* reply was given, not that the reply was unambiguous affirmative
consent to the specific, irreversible action at hand. Worth a prompt-level rule:
never treat a reply to a compound or tangential question as consent on an irreversible
action — split the question, or restate the specific ask ("just to confirm: cancel
AVS-99004050, $53.99 penalty — yes or no?") before proceeding. Two minor, lower-severity
notes from the same session: the bot re-displayed the customer's own already-verified
email back to them mid-flow rather than requiring it retyped (harmless — same
reservation, already verified — but worth a glance); and the disambiguation flow
(cancel vs. early return) and the post-cancellation summary both performed correctly
under real, confused human typing, which is good evidence the earlier disclosure fixes
are holding under pressure.

---

---

---

---

## Cleared

### 17. No authoritative source for "today" — FIXED
Instructions are now a callable regenerated every turn, injecting real UTC time (a static
string would go stale once the process outlived its start day). `build_quote()` gained a
real-time floor rejecting targets already past relative to reality — the reservation's own
stored dates are months stale and are not a substitute for knowing the actual date.
Verified live: "extend to the 20th" on a June reservation now returns "June 20, 2026 has
already passed... the new date needs to be in the future from today (August 6, 2026)"
instead of quoting a backdated charge. Tests pin `now=EXTEND_TEST_NOW` so the suite
doesn't become its own time bomb; new guard mutation-verified.

### 18. Compound confirmation question produced ambiguous consent — FIXED
Added an explicit rule: never treat a reply to a compound or multi-part question as
consent to cancel — re-ask the cancel question alone, with the specific figures. Verified
on the exact original input: "yeah do the original" now collects the email rather than
cancelling (`consumed=False`), where it previously cancelled a real reservation.

### 5. Bot identity not disclosed until challenged — FIXED
The CLI greeting now opens with "I'm Avis's automated assistant", and the instructions
already required disclosure on being asked. Verified in the live cancel E2E runs.

### 6. Early-return question escalated instead of answered — FIXED
Fixed by design in the Cancel build: `estimate_cancellation` flags in-rental reservations
with `requires_disambiguation` and carries the early-return alternative (kb_can_04: no
fee, charges follow actual time, handled at the counter). Verified live — "i just dont
need the car anymore" got the correct answer, nothing was cancelled, no escalation.

### 14. Card last-four disclosed to an unverified caller — FIXED
Found during the 2026-08-05 bug sweep. Asked to extend, the agent replied *"I'll charge
your card ending in 1122"* with `verified_email` still `None`. Reads are open, so anyone
holding a reservation ID could supply **any** email and be read the card's last four.

Root cause was an ambiguous instruction — *"before the customer is verified (they give the
email on file)"* reads as though supplying an email **is** verification, when verification
only actually happens when a write succeeds against the email on file.

Fixed by omission rather than instruction: `lookup_reservation` now returns
`"withheld until verified"` for the card unless `session.verified_email` is set. What the
model never receives, it cannot disclose. Retested — the agent now says "your card on
file" with no digits.

### 15. Unparseable datetimes escaped as ValueError — FIXED
`build_quote("June 17th")` raised a raw `ValueError` out of the tool instead of a
recoverable `FlowError`. Six of eight malformed inputs crashed, including `2026-13-45` and
`17/06/2026`, which slipped past the first fix via an early return in the date-only
branch. Customers say "June 17th" and the model may pass it straight through. All paths
now validate and raise `FlowError`, which the agent recovers from by re-asking.

### 16. `test_hard_handoff_blocks_every_action_tool` was a fake pass — FIXED
The test looped over tool names and then asserted on `_blocked()` directly, never invoking
a tool — it would have passed with every guard removed. Now calls each tool's real
implementation via `__wrapped__`, and mutation-tested: deleting the guard from
`confirm_extension` makes it fail, restoring it makes it pass. Third instance of this
class of bug (see #11); worth checking for deliberately.

### 12. Escalation is advisory, not terminal — FIXED
`handed_off` is now a distinct terminal state that action tools check via `_blocked()`.
Split into `kind="hard"` (terminal: cancel, upgrade, repeated verification failure) and
`kind="assistive"` (revocable: customer can't find their reservation ID). Verified live —
after a cancel request the agent refuses a follow-up extension instead of servicing it.

### 13. Fabricated access-control policy — FIXED
Root cause addressed: instructions now state **affirmative permission** (vehicle, dates,
locations, daily rate, amount charged, membership, status are all shareable; only
`card_last_four` needs verification) instead of leaving a vacuum the model filled with
invented rules. Added a ban on describing its own limits as "Avis policy" or "for security
reasons" unless a retrieved article says so. Replayed the exact broken conversation: the
agent now answers the question directly.

### 2. Hallucinated *reasons* for real numbers — FIXED
Instructions now forbid inventing a reason as explicitly as inventing a number: if no
retrieved article explains a fee, say it's the standard fee and that detail isn't
available. Verified — the late-fee explanation is now sourced from `kb_fee_02` rather than
the fabricated "more than 24 hours past your original due time" rule.

### 3. Over-escalation on answerable questions — FIXED
The agent's remit is now "look up a reservation and answer questions about it" rather than
extend-only. Read-only questions about a loaded reservation are answered, not escalated.

---

## Adversarial testing pass (2026-08-06) — closed

Ten strict-xfail tests from an external testing pass. Six fixed and graduated, one fixed
in part, three deferred with the reason attached to the live test. Full write-up in
DECISIONS.md §2 (xfail discipline, retrieval limits), §3 (gates), §5 (client envelope),
changelog. Fixed: repriced quote chargeable in the same turn; mid-rental cancel
disambiguation never read by the write (`resolve_cancel_intent`); no post-write
reconciliation on extend; incomplete `success: true` write reported as complete; client
trusted HTTP status over the response envelope; KB false positives on uncovered topics;
`escalate_to_human` docstring still naming cancel as forbidden. Deferred:
affirmative-consent detection, bare-CVV redaction, DST-aware branch time.

**Carried forward for the stress-test session:** the pass's own headline defect was
methodological — a module-level `xfail` reported four crashing tests as findings. When
scripted coverage and live sessions disagree about whether something is exercised, trust
the live session. Item B below is unchanged by any of this.

---

## Final testing phase (2026-08-06 and ongoing)

The librarian work (Phases 0–5, PR #11, merged to main) and observability infrastructure
(codex branches #12/#13, sandbox #14) are complete. The **comprehensive testing pass**
now runs to gather evidence for flag-readiness decisions and catch any remaining bugs.

Detailed scope is in DECISIONS.md §8A and §8B. Tools available:

- `dev/run_local.py` — interactive offline sandbox, real agent/prompt/gates against
  synthetic reservations that real accounts cannot reach (genuinely mid-rental, pre-pickup,
  cancelled, etc.).
- `codex/test-harness-experiments` (PR #13) — Hypothesis property tests and rule-based
  state machine; high-volume runs will find edge cases. Also provides isolated per-run
  test artifacts with correlation IDs and centralized redaction.
- `codex/test-run-artifacts` (PR #12) — subset of #13; close as superseded after #13
  merges.

### Shadow-mode data collection (runs concurrently)

**Target:** evidence for whether `KB_RETRIEVAL_MODE`, `FLEXIBLE_DATE_ALTERNATIVES_MODE`,
`IN_FLOW_UPGRADE_OFFER`, and `CANCEL_RETENTION_PROMPT` should stay off or flip default
before submission.

- **KB_RETRIEVAL_MODE**: 150–200 realistic customer queries, stratified by category/homonym/
  uncovered/paraphrase. Compare lexical served vs. librarian finalized; track agreement,
  hallucination, latency, no_coverage precision. Decision rule: flip to shadow/librarian
  only if disagreement is >90% AND hand-review favors the librarian.
  
- **FLEXIBLE_DATE_ALTERNATIVES_MODE**: Shadow-run alternative-date quoting in parallel,
  measure cost/availability deltas without surfacing. Decision: stay off unless
  alternatives are cheaper/available >40% of the time.
  
- **IN_FLOW_UPGRADE_OFFER** / **CANCEL_RETENTION_PROMPT**: Scripted multi-turn conversations
  via sandbox, count trigger frequencies. Both are UI/UX levers, not safety gates — expect
  to stay off for submission; ops decides live if they're ever used.

### Comprehensive testing tracks (run independent pieces in parallel)

1. **Property/state-machine at scale** — raise `max_examples`/`stateful_step_count` to
   500+/100×100, overnight, looking for shrinkable failing cases.
2. **Adversarial conversational** — scripted multi-turn: prompt injection, double-confirm,
   mixed intents, assert nonexistent policies, handoff boundary testing.
3. **Sandbox scenario end-to-end** — 20–30 full conversations each through all 7
   scenarios; real tone/hallucination/gate-leak testing.
4. **Mutation testing on money code** — break each safety invariant in policy.py,
   extend_flow.py, cancel_flow.py; confirm a test fails for each.
5. **Concurrency/load sanity** — SQLite session store and JSONL logs under concurrent use.
6. **Finalization checklist** — README accuracy, env.example completeness, DECISIONS.md
   updates, flag defaults verified.

### A. Upsell evaluation (deferred for now)
`_upgrade_offer()` currently fires only in extend when charging a late fee; it's not
wired into Cancel. Scope to evaluate later if time allows: wire into Cancel where fee
avoidance is the stated motive; ensure handoff carries both upgrade and pending change;
watch for upsell-as-obstacle failure mode. All behind `IN_FLOW_UPGRADE_OFFER`, default
off, shadow-logged.

### B. Known open review items (to be cleared or deferred in this pass)
Items #1, #4, #7–#11 remain unresolved (see "Open" section above). Track outcome in
final session report: fixed, deferred, or accepted as-is.
