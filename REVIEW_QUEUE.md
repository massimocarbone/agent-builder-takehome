# Review queue

Small issues found in passing, left for a human pass rather than fixed inline. Each item
names the file, the problem, and the decision to be made — not a patch to rubber-stamp.

Cleared items move to the bottom with their resolution.

---

## Open

> Items 2–8 all come from one unscripted human session on 2026-08-05 (`AVS-48372915`).
> A single real conversation surfaced more than four scripted adversarial tests did —
> worth repeating before every merge from here on.

### 2. Hallucinated *reasons* for real numbers — highest severity
**Where:** prompt guardrails in `src/agent.py`; `search_policy` usage.

Asked why a $29 late fee applied, the agent answered: *"because the new return time is
more than 24 hours past your original due time. Even on extensions, some locations add
this fee, especially if the new return stretches beyond their standard grace periods."*

**None of that exists in any article.** The real reason is mundane: the rental was due
2026-06-09 and is already ~2 months overdue, so the fee is simply the standard late fee.

The existing rule — "never invent prices, fees, policies" — covers *figures*, and the
figures were all correctly tool-sourced. It does not cover **causal explanations of
figures**, which is arguably worse: a customer who believes "return within 24 hours and I
avoid it" makes a decision on invented policy. Fix is a prompt constraint plus a
disclosure norm: explanations must be quoted or paraphrased from a retrieved article, and
when no article explains it, say so ("that's the standard late fee — I don't have detail
on why it applied to this booking").

### 3. Over-escalation on questions it can already answer
The customer asked to *check the status* of the reservation and later *what it cost*. The
agent escalated for the first and refused the second ("I'm not able to provide full
pricing breakdowns") — while holding a tool result containing `daily_rate`,
`total_charged`, dates, vehicle, and status.

Burning a human handoff on data already in hand is the expensive-but-safe failure
direction, and it's the one that shows up in cost-per-contact. The agent should answer
read-only questions about a reservation it has loaded. Escalate on *actions* it can't
take, not *facts* it already has.

### 4. No frustration / no-progress escalation trigger
The session degenerated into `no` → `no` → `what` → `no i wann wtd`, with the agent
returning cheerful non-answers each time. The tester's own note: *"at this point we
should have likely had a real person step in."* Correct.

Every current escalation trigger is **event-based** (verification failures, API errors,
explicit out-of-scope). Nothing watches the *conversation* for degradation: repeated
short/negative turns, no state change across N turns, profanity, or explicit
frustration. This is a cheap, high-value addition and it belongs in code next to the
other triggers, not in the prompt.

### 5. Bot identity not disclosed until challenged
The customer asked *"youre not a real person???"* partway through. Beyond the trust cost,
disclosure is a legal requirement in some jurisdictions (e.g. California's B.O.T. Act).
The opening line should identify the agent as automated.

### 6. Early-return question answered with an escalation instead of the policy
*"can i just return it now?? i dont need it anymore"* — this is precisely the
early-return-vs-cancel case in DECISIONS.md §3. The agent said it couldn't help and
offered a representative, without searching. `kb_can_04` has the useful answer: **no fee
to return early**, charges follow actual rental time, with some rate plans not refunding
unused days. A good answer here saves the handoff entirely.

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

---

### 1. Agent volunteers the remaining verification-attempt count
**Where:** `src/extend_flow.py`, `commit_extension`, the `VERIFICATION_FAILED` branch.

The customer-facing message says `Attempt N of 3`, and the model relays it ("you still have
two more tries"). Friendlier, but it also tells someone probing a reservation ID exactly how
much runway they have before the session escalates.

**The call:** keep it (transparency, and the reservation ID is the weaker secret anyway) or
drop the count and let escalation arrive unannounced. Either is defensible; it's a
security-vs-UX judgment, which is why it's here rather than silently decided.

---

## Cleared

_(none yet)_
