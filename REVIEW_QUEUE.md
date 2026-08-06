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

## Planned for the final session (2026-08-06)

Last dedicated hour: **stress testing and trying to break the system**, plus evaluating
upsell logic. Recording scope here so the session starts with a plan rather than a warm-up.

### A. Upsell evaluation — the gap to close first
`_upgrade_offer()` currently fires in **one** place: `extend_flow.build_quote`, and only
when a standard member is being charged a real late fee. It is **not wired into Cancel at
all** (verified). So the scenario named for evaluation — *"a customer cancelling to avoid
late fees"* — currently gets no offer, which is exactly the case where a membership
upgrade is most relevant: Preferred members are exempt from late fees (`kb_pref_02`), so
someone cancelling *because of* a late fee may be better served upgrading and keeping the
booking.

Scope to evaluate (all behind `IN_FLOW_UPGRADE_OFFER`, still default off, shadow-logged):
- Wire the offer into the Cancel path where the customer's stated motive is fee avoidance.
- Confirm the handoff carries **both** the upgrade intent and the pending change, so one
  representative finishes both in a single interaction (the original design intent — the
  bot never calls `/customers/{id}/upgrade` itself).
- Watch for the failure mode that matters: an upsell that reads as an **obstacle** to
  cancelling. Offer once, accept "no" immediately, never gate the cancellation on it.
  This is the same restraint rule as `CANCEL_RETENTION_PROMPT`, and the trust cost of
  getting it wrong is higher than the revenue upside of getting it right.

### B. Stress-test targets (highest value first)
1. **Consent boundaries** — the class our code gates provably cannot close (#18). Compound
   questions, mid-sentence reversals, "wait no", sarcasm, consent given then withdrawn.
2. **Date reasoning** — now that time is grounded (#17), attack it: relative dates
   ("next Tuesday"), ambiguous ones ("the 20th"), DST/timezone edges, far-future targets.
3. **Cross-reservation state** — switching reservations mid-flow, mixing IDs, retrying
   after a switch. The isolation guard is new (audit #1) and lightly exercised live.
4. **Handoff terminality** — requests after a hard handoff, arguing with it, new
   reservation IDs post-handoff.
5. **Hallucination pressure** — demand reasons for fees, invent policies and ask for
   confirmation, ask about uncovered topics (insurance, pets) where retrieval returns
   plausible-but-wrong articles (#10).
6. **Adversarial identity** — probing reservation IDs, claiming employee authority,
   guessing emails to the lockout boundary.
