# Design Decisions — Avis Servicing Agent

> **Living document.** Written during scoping, updated as decisions change during the build.
> Becomes the basis for the final README. Anything marked ❓ is unresolved and needs the API
> reference or a build-time discovery to settle.

---

## 1. Scope decision

**Building, in this order: Extend → Cancel → Modify (time-only).**
**Explicitly cut: Upgrade. Stretch goal: Modify with `new_return_location`.**

### Why this set

Two lenses were used, and they agree:

**Volume lens (which workflows cover the most support contacts).**
Avis's stated goal is handling a meaningful share of servicing volume. Contact-reason data
for rental companies consistently ranks booking changes at or near the top of servicing
requests, with cancellation also high-frequency. Extensions are frequent, commercially
valuable (more rental days = revenue), and today are high-friction — several major brands
still route extensions through a phone call or the counter. Tier upgrades are not a common
inbound support reason in any travel vertical; they're an upsell, not a servicing queue.

Extend + Cancel + Modify plausibly covers the large majority of post-booking servicing
intent. Upgrade is the clear tail.

> ⚠️ **Assumption, flagged per the brief.** No rental company publishes a contact-mix
> breakdown by request type — not Avis, Hertz, or Enterprise. This ordering is informed
> inference from contact-reason rankings, extension-friction evidence, and adjacent travel
> verticals, not measured fact. The first thing I'd ask Avis for is their intent-tagged
> contact logs; that single dataset outranks all of it.

**Risk lens (which workflows are architecturally distinct).**
Extend and Cancel sit at opposite ends of the risk spectrum:

| | Extend | Cancel |
|---|---|---|
| Revenue direction | Positive | Negative |
| Reversibility | Effectively reversible | Irreversible |
| Policy complexity | Low | High (refund/penalty windows) |
| Interesting question | Can the agent own this end-to-end? | How much should the agent be allowed to do at all? |

Building one of each proves the design generalizes across capability classes. Building
Extend + Modify would prove one pattern twice.

### Why this build order

- **Extend first** — simplest complete path; establishes the API client, error handling,
  confirmation gate, and agent scaffolding that everything else reuses.
- **Cancel second** — deliberately chosen as the second build *because* it's different.
  It stress-tests whether the abstractions from Extend actually generalize, while there's
  still time to fix them. It also forces the policy/knowledge-base retrieval path to exist.
- **Modify last** — structurally near-identical to Extend (`new_pickup_datetime` /
  `new_return_location` vs `new_return_datetime`, otherwise shared params), so it's the
  cheapest to add and the safest thing to leave unfinished. "Ran out of time on Modify"
  is a fine answer; "ran out of time on Cancel" would mean never demonstrating policy
  reasoning.

Rough time budget: Extend ~30%, Cancel ~25–30%, Modify ~15–20%, remainder for README,
logging, and demo prep.

### What was cut and why

- **Upgrade (Standard → Preferred)** — lowest inbound support volume of the four. Better
  positioned as a contextual upsell offered *inside* an extend/modify flow than as its own
  servicing workflow. Low information gain per hour spent.
- **Modify with location change** — `new_return_location` is not additive complexity. It
  pulls in availability at a different branch, likely one-way fees, and possibly fleet-class
  implications. Scoped out by default; time-only Modify is the target. ❓ Confirm against
  `docs/api-reference.md`.
- **Front-end** — a CLI loop is a sufficient demo. Agent returns are kept structured
  (dicts, not prose blobs) so a UI would be a thin wrapper if time allowed.

---

## 2. Architecture

Three layers, deliberately keeping the LLM out of the parts that touch money.

### Layer 1 — Deterministic API client (not agentic)

Typed function per endpoint. No freeform request construction by the model.

- Retry with exponential backoff on transient failures
- **Error classification**: retryable (5xx, timeouts, connection errors) vs terminal
  (400 validation, 401/403 auth, 404 not found). Never retry a malformed request.
- Structured logging on every call: endpoint, params (secrets redacted), status, latency,
  attempt number, outcome
- ❓ Determine actual failure modes from `docs/api-reference.md` — the brief says to treat
  it as "a real, imperfect API," which implies deliberate flakiness to handle.

**Alternative considered and rejected:** an agent that constructs API calls ad hoc, so the
system could adapt automatically as Avis changed their endpoints or policies. Rejected
because this agent mutates real bookings and real money — improvised requests are hard to
test, hard to guarantee, and a poor trade against reliability. Noted as a future
consideration, not a v1 design.

### Layer 2 — Agents

Triage agent → specialist handoff, using the OpenAI Agents SDK's native handoff pattern
(idiomatic to the recommended framework, not a bespoke orchestration layer).

- **Triage agent** — identifies intent, collects reservation ID and the email on file
- **Extend specialist** — availability → quote → confirm → write
- **Cancel specialist** — policy lookup → quote refund/penalty → confirm → write
- **Modify specialist** (if reached) — reuses the Extend path with different params

### Layer 3 — Knowledge / retrieval

`data/knowledge-base/articles.json` exposed as a retrieval tool for policy questions
(cancellation penalties, extension rules, grace periods).

**Deliberately not using a vector database.** The corpus is small (30 articles); simple
keyword or lightweight embedding search is sufficient and adds no operational surface.
Calling this out explicitly because "why no vector store" is a predictable question — the
answer is that it wasn't needed at this corpus size, not that it was overlooked.

**Retrieval must rank by `authority` and `last_updated`, not similarity alone.** The
corpus contains a deliberate contradiction:

| Article | Authority | Updated | Claim |
|---|---|---|---|
| `kb_ext_01` | `official-policy` | 2026-04-10 | Grace period is **30 minutes** |
| `kb_fee_01` | `legacy` | 2024-09-12 | Grace period is **2 hours** |

A naive keyword match on "grace period" ranks the stale `legacy` article first on term
frequency and tells the customer something false by 90 minutes. Articles carry
`authority` (`official-policy` > `help-center` > `legacy`) and `last_updated` precisely so
retrieval can break ties on trustworthiness rather than wording. Ranking is therefore
authority-weighted, and any answer drawn from a `legacy` article is either suppressed or
surfaced with its date. This is the same source-of-truth principle as §5's "API wins,"
applied *within* the knowledge base.

**`kb_sup_01` corroborates the escalation rules** independently arrived at in §3: it names
scheduling conflicts, high-value or complex reservations, payment issues, and "any change
where the right outcome is unclear" as representative-handled. Where published policy and
our design agree, the design cites it.

### Retrieval: lexical + authority ranking, not embeddings

Scoring is term overlap weighted toward `title` and `category`, multiplied by an authority
weight (`official-policy` 1.0, `help-center` 0.7, `legacy` 0.25), with `last_updated` as
the tiebreak. When a `legacy` article and a higher-authority article both match within the
same category, the legacy one is suppressed rather than shown alongside — the model never
has to adjudicate a conflict it can't see the provenance of. Below a score floor, the
agent says it has no policy on the question and offers a human rather than improvising.

**Why not embeddings.** Recall is not the failure mode at this corpus size — 30 articles
across 7 categories, with titles written in customer vocabulary, are reliably found by
lexical matching. The failure mode that actually costs something is retrieving the
*wrong-authority* article and quoting a grace period that is wrong by 90 minutes. Lexical
scoring also stays deterministic, which turns that trap into a regression test: assert
that "grace period" returns `kb_ext_01` and never `kb_fee_01`. That assertion is much
harder to write against embedding similarity. Embeddings are the documented scale-up path
for a 3,000-article corpus, not this one.

### Retrieval provenance is a product feature, not just a debug aid

Every retrieval logs the article IDs it returned, their authority, and their
`last_updated`, alongside the answer that was given. That trail exists first so a wrong
answer can be traced to a specific article after the fact.

But it also inverts into something more valuable to Avis: **the agent becomes an
instrument for finding stale documentation.** If customer complaints, escalations, or
corrections cluster around a particular article ID, that article is the problem — and the
log says so directly, with a citation. A help centre of any size has no other cheap signal
for which of its pages have quietly gone out of date; here it falls out of normal
operation. The `legacy` grace-period article is the worked example: it is exactly the kind
of page that would surface at the top of that report on day one.

### What the knowledge base is *not* allowed to do

Retrieval answers policy questions in prose. It never supplies a number that drives money.

The moment a retrieved article becomes the source of a dollar figure, the model is reading
"a penalty equal to one day's rate" and doing arithmetic against `daily_rate` — the exact
LLM-touching-money pattern the rest of the architecture exists to prevent. Instead a
deterministic policy function computes the figure, and the article is attached as the
*citation* justifying it. Code can be unit-tested; a paraphrase cannot.

**Built and verified as designed** (`src/kb.py`). IDF-damped term overlap, weighted
toward title/category, times the authority multiplier; legacy-article suppression when a
higher-authority match exists in the same category. One deviation from the original plan:
plain term-frequency scoring let "return" — present in nearly every article — swamp the
discriminating word in a query like "return early," so IDF weighting was added after the
first test run failed honestly. Regression tests assert the grace-period trap under four
phrasings and pass; live conversation confirmed the agent answers 30 minutes (official),
never the legacy 2 hours, and cites `kb_fee_02` for the $29 fee rather than inventing a
reason for it.

### Testing strategy: fixtures from real payloads, never invented from scratch

Several cancel-policy branches (pre-pickup cancellation, non-refundable rates, pay-at-
counter) are unreachable through any test reservation — see §5. Rather than write flow
logic that would ship having never executed, `tests/fixtures.py` builds test data by
capturing a live API response once and mutating only the field the branch under test
needs (e.g. shifting `pickup_datetime` into the future for the pre-pickup branch). A
fixture invented from imagination tests the author's assumptions, not the system.

Two deliberately separate seams: patching `avis_client.requests.request` exercises the
*real* client — retries, backoff, idempotency headers — against a scripted transport;
patching a client function directly skips the client and tests flow logic alone. Mixing
these up produces a test that looks like coverage but proves nothing — this happened
twice in this build (a handoff-guard test that asserted on internal state instead of
calling the tool it claimed to test; a card-disclosure test whose stub was never reached
because `agent.py` had imported the function by bare name, so patching the module
attribute it was defined on did nothing). The second was only caught by an external
audit. The standing discipline going forward: **when a test patches something, assert
that the patch was actually reached**, not just that the final outcome looks right — a
mutation check (deliberately break the guard, confirm the test then fails) is cheap
insurance against writing a test that would pass regardless.

---

## 3. Cross-cutting design rules

### Confirmation gate before any mutating call

**Quote first → show the customer the money impact → get explicit confirmation → write.**

Single most important pattern in the build. Covers both halves of Avis's stated concern:
revenue protection and customer trust. No write endpoint is ever called without the
customer having seen and accepted the price/penalty consequence.

**Enforced in code, not in the prompt.** The gate lives in `extend_flow.commit_extension`,
which refuses to charge unless a quote the customer was shown is staged in session state.
A model that is confused, jailbroken, or simply told "just charge it, skip the quote"
still cannot reach the write — it gets a refusal back as a tool result. Prompt
instructions restate the rule for conversational quality; they are not what enforces it.

The API's payment requirement turns out to strengthen this: `extend`/`modify` need
`payment.cvv` and `payment.billing_zip`, which only the customer can supply. Handing over
the CVV *is* an affirmative act of consent, taken after the total was quoted — so the
gate has a second, independent lock that isn't in the model's control either.

Money-touching logic therefore lives in plain Python (`extend_flow.py`), separate from the
agent definition, so it can be read, reviewed, and tested with no LLM in the loop.

**Consent is measured in conversation turns, not tool calls.** An external audit found
that nothing stopped the model from calling `quote_extension` then `confirm_extension`
inside a single run — before the customer had seen a single word of the total. The gate
originally checked "does a quote exist," which "does a quote exist that the customer
replied to" is a different, stronger claim. Fix: `ServicingSession` counts turns, each
`PendingQuote` records the turn it was staged on, and `commit_extension` refuses unless
the current turn is strictly later. This encodes what "the customer agreed" structurally
means — the quote went out, control returned to the human, and they replied — rather than
a wall-clock proxy (e.g. "quote must be N seconds old") that a fast customer or a slow
test would each trip in the wrong direction. Consequence for future work: **every write
flow, Cancel included, must quote and charge on separate turns.** This is now a load-
bearing constraint on the architecture, not an implementation detail.

**A quote is single-use.** `PendingQuote.consumed` is set on a successful write and
checked before the next one. Without it, a retried tool call or a replayed turn after a
provider hiccup could charge twice — each write mints a fresh `Idempotency-Key`, so the
API's own replay protection (§2, idempotency) does not cover a second *logical* charge,
only a retried *identical* one.

**Verification and a staged quote are scoped to one reservation, not one conversation.**
Switching reservations mid-conversation used to carry `verified_email`, `pending_quote`,
and `failed_verifications` across the switch — so proving you knew reservation A's email
could disclose reservation B's card digits, and a quote priced for A could be committed
against B. `ServicingSession.load_reservation()` clears all three whenever the incoming
reservation id differs from the one already loaded. `handed_off` deliberately does **not**
reset on a switch — a terminated conversation must not reopen just because the caller
names a different booking.

### Identity verification

The API enforces this naturally: reads (`GET /reservations/{id}`) are open, writes require
the email on file. That boundary is the auth gate — the agent must collect and validate
the email before attempting any state change, and must fail gracefully (not leak whether
the email was wrong vs the reservation absent) ❓ — confirm what the API returns on
mismatch.

### Error asymmetry — bias toward escalation

The two failure directions cost very different amounts. Wrongly escalating to a human costs
a few dollars of handling time. Wrongly cancelling a booking, double-charging, or applying
the wrong penalty costs revenue, a refund, and customer trust — the exact things Avis said
they don't want at risk.

The agent is therefore **deliberately biased toward escalation** wherever confidence is low.
This is a design principle, not an artifact of prompt tuning, and it's the answer to "where
do you draw the line": the line sits wherever the downside is asymmetric.

### Whose agent is this? — revenue vs. trust

Avis is the buyer; the renter is the user. Their interests mostly align, but diverge on
Cancel. The position taken here:

- **Revenue opportunity lives in Extend and Modify**, where interests are aligned. A
  customer extending three days genuinely benefits from being told the weekly rate is
  cheaper. This is where the conversational surface earns its keep commercially.
- **Upgrade = membership upgrade, offered in-flow, finalized by a human.** The API's
  `/customers/{id}/upgrade` is a *membership* upgrade (standard → `avis_preferred`), not a
  vehicle-class change. It returns here as an in-flow upsell inside extend/modify: Preferred
  members are exempt from late-return fees, so where an upgrade would offset costs in the
  options being presented, the agent surfaces that ("upgrading would waive this $X late fee")
  and answers questions about the upgrade if asked. If the customer wants to proceed, the
  agent **escalates to a human to finalize the upgrade** — and passes along the full context
  it collected (the extend/modify options quoted, dates, prices), so the human can complete
  the upgrade *and* the pending booking change in one interaction rather than forcing the
  customer to start over. The bot never calls the upgrade endpoint itself.
- **On Cancel, deliberately restrained.** Offer an alternative (date change instead of
  cancellation) **once**, accept a "no" immediately, and never gate the cancellation on it.
  A retention attempt that becomes an obstacle is precisely how these agents destroy trust,
  and the brief names trust as a hard constraint.

### "Cancel" is ambiguous mid-rental — read state before believing the verb

**The finding.** The knowledge base and the API disagree about what cancellation *is*.
`kb_can_01` says reservations are cancelled "before the rental begins," and `kb_can_04`
says explicitly that returning a car early is **not** a cancellation. But every reservation
in the test set has `status: active` — the customer physically holds the vehicle — and the
API cancels them regardless, charging a penalty of one day's rate.

**Verified against the API:** shortening a rental is impossible. `modify` with an earlier
`new_return_datetime` returns `400 INVALID_CHANGE` ("does not extend the rental or change
location"), while the same call with a later datetime succeeds. There is no early-return
endpoint. Early return happens physically, at the counter, outside this API entirely.

**So the two real outcomes for a customer holding a car diverge on money:**

| | Cancel via API | Physically return early |
|---|---|---|
| Fee | Penalty = one day's rate | No fee (`kb_can_04`) |
| Refund | Prepaid − penalty | Based on time actually rented; some rate plans refund nothing |
| Vehicle | Still in their possession, now with no reservation | Returned |
| API action | `POST /cancel` | None exists |

Which is cheaper depends on the rate plan and how much of the rental has elapsed. **The
agent must not choose on the customer's behalf.** A customer who says "cancel" but means
"I'm done, I'll bring it back" would be charged a one-day penalty for a word choice — a
trust failure invisible to anyone not looking for it.

### Cancel decision tree

Branch on live reservation state, never on the customer's verb.

```
"cancel" intent
│
├─ 1. Look up reservation FIRST. Read status, pickup_datetime, current_return_datetime.
│
├─ 2. status != active
│     → API returns 409 RESERVATION_NOT_ACTIVE. Already cancelled, or completed.
│       Explain; do not retry. Escalate if the customer disputes it.
│
├─ 3. pickup_datetime is in the FUTURE  →  a true cancellation
│     ├─ >48h before pickup   → estimate: no penalty, full refund of prepaid
│     └─ ≤48h before pickup   → estimate: penalty = one day's rate
│     → estimate (labelled) → confirm → cancel → compare actual to estimate
│       → material variance escalates (§3 handoff rules)
│
└─ 4. pickup_datetime is in the PAST  →  customer has the car. Do NOT assume cancel.
      Disambiguate before acting:
      ├─ "I'll bring it back early / I'm done with it"
      │     → early return. NO API action. Tell them to return it; charges follow
      │       actual rental time, no fee. Cancelling here would ADD a one-day penalty
      │       and leave them holding an unreserved vehicle.
      ├─ "Cancel the whole thing, I don't want to be charged"
      │     → explain what cancel actually does: penalty of one day's rate, refund of
      │       the remainder, AND the vehicle must still be returned. Show the money.
      │       Explicit confirmation, then cancel.
      └─ ambiguous, disputing charges, or overdue beyond policy
            → escalate (kb_elig_01: overdue reservations may require a representative)
```

### Situations the test data does not cover

All six test reservations are `active`, prepaid, and past their return date, which means
they exercise exactly one branch of the policy. Everything below is designed for but
unobserved — reviewers will test on reservations we haven't seen:

- **Pre-pickup cancellation, >48h out.** The full-refund/no-penalty branch has never been
  observed. Implemented from `kb_can_01`, labelled as an estimate.
- **Pre-pickup cancellation, ≤48h out.** Same formula as observed, different trigger.
- **Non-refundable prepaid rates.** `kb_can_01` names them as an exception — and the
  reservation payload exposes **no rate-plan field**, so the agent cannot detect one. This
  alone is sufficient reason the pre-confirmation figure is always an estimate.
- **Pay-at-counter with no prepayment.** `kb_can_02` says there is simply nothing to
  refund. All six test reservations carry a `total_charged`, so this is unobserved.
- **No-show** (`kb_can_03`) — pickup time passed, vehicle never collected.
- **Already-cancelled / completed rentals** → `409`. Unreachable in testing because writes
  are ephemeral and never persist.
- **"Cancel" meaning a different, future reservation** the customer also holds.

**Penalty formula, verified.** Across all six reservations the API returned
`penalty == daily_rate` exactly, and `refund == prepaid − penalty`, matching `kb_can_01`.
That makes the deterministic estimator accurate for the observed branch — and the
unobserved branches above are precisely why it stays an estimate with a variance
escalation behind it.

### Graceful out-of-scope decline

Since Upgrade-as-workflow is cut, the agent must *recognize* an upgrade request and hand
off cleanly rather than hallucinate a capability it doesn't have. Treated as a feature to
build and demo, not a gap.

### Handoff to human

Explicit conditions where the agent stops and escalates rather than proceeding:

- **Repeated verification failure** — 2–3 `403 VERIFICATION_FAILED` on a write. (Note:
  the API has no email-validation read; the write itself is the check, so this is
  verify-on-first-write, not a separate validation step.)
- **Cancel penalty variance** — the API has **no cancel quote** (`/quote` only accepts
  `extend`/`modify`), so the pre-confirmation penalty estimate comes from the knowledge
  base, presented explicitly as an estimate ("final amount confirmed on cancellation").
  If the actual `cancellation_details` from the cancel call diverge materially from the
  estimate the customer agreed to, **escalate to a human agent** rather than silently
  proceeding — a charge the customer never saw defeats the confirmation gate.
- **Membership-upgrade acceptance** — see "Whose agent is this" below; the bot never
  finalizes the upgrade itself.
- **Ambiguous intent** after several turns; **any terminal API error on a write**
  (`PAYMENT_DECLINED`, `RESERVATION_NOT_ACTIVE`, etc.) that the agent can't resolve
  by re-collecting input.

**Escalation is a real terminal state, enforced structurally.** A live human-run session
on 2026-08-05 caught the agent announcing a handoff and then answering the customer's
question itself 19 seconds later. The cause: `escalated` was written in three places and
read in none — it was a sentence the model narrated, not a state anything checked. Fixed
by splitting escalation into two kinds:

- **`hard`** (verification lockout, terminal API error, an action this agent must not
  take — cancel, upgrade, location change) sets `session.handed_off = True`. Every action
  tool checks this before doing anything and refuses if set; the refusal is structural
  (`_blocked()` in `agent.py`), not a instruction the model is trusted to follow. A
  follow-up audit found two more paths that set `escalated` but not `handed_off`
  (`classify_failure` and the verification lockout itself) — same bug, reached a
  different way; both now set the terminal flag directly.
- **`assistive`** (e.g. "the customer can't find their reservation ID") stays revocable.
  In the same 2026-08-05 session the customer found their ID one turn after escalating —
  hard-terminating there would have been its own bad experience.

**The handoff payload carries the verbatim transcript, not just the model's own summary.**
Given the model has repeatedly invented details with full confidence (see the fabricated
late-fee reason and the fabricated "Avis policy" on disclosure, both below), its own
summary of the conversation is the least trustworthy artifact available to hand a human.
Card codes and card-like digit runs are scrubbed on the way in — customers type CVVs
directly into chat.

### The agent's remit is answering, not just acting — and never inventing why

A live session on 2026-08-05 found the agent refusing to state a customer's own return
date ("I am only permitted to help extend your rental... according to Avis policy"),
then disclosing it two turns later once the customer said "extend." Investigation found:
zero of the 30 knowledge-base articles mention any disclosure or verification-gating
policy, `search_policy` was never called during the exchange, and the agent's own
instructions at the time already permitted exactly what it refused. **The refusal was
the hallucination; the eventual disclosure was correct.**

Root cause: the agent had exactly one sanctioned capability (extend), so any other
request fell into a vacuum the model filled with an invented rule. Fix was to state
**affirmative permission** rather than only prohibition — vehicle, dates, locations,
daily rate, amount charged, membership, and status may always be shared for a loaded
reservation; only the card's last four requires verification — and to forbid describing
the agent's own limits as "Avis policy" unless a retrieved article actually says so.
This generalizes the standing rule from "never invent a price" to "never invent a
reason": the same session found the agent explaining a real $29 late fee with a
completely fabricated 24-hour rule. Both are now covered by one instruction: an
explanation is only as trustworthy as the number it explains, and needs the same
sourcing discipline.

**Consequence for scope.** The fix was not "make the prompt stricter" — it was "give the
agent something legitimate to do with every question it's asked." A rigid, single-verb
agent didn't produce safety; it produced hallucinated scope. The gates that actually
matter (quote-before-charge, verification-before-write, turn-boundary consent) are
enforced in code and hold regardless of how broad the agent's conversational remit is —
so widening what it can *discuss* costs nothing the architecture depends on.

### Not hard-coding to test reservations

The brief warns the agent will be tried on unseen reservations. No test-account-specific
logic anywhere.

---

## 4. Speculative features & the flag policy

### The policy

Several product decisions in this build are **assumptions about what Avis wants that cannot
be validated without their data.** Rather than guess and hard-code, every speculative
behavior is:

1. **Behind a feature flag**, configurable per-deployment
2. **Defaulted off** where the behavior is commercially aggressive or unvalidated
3. **Shadow-logged even when off** — the agent computes what it *would* have done and
   writes it to the log without surfacing it to the customer

**One honest caveat on point 3.** Shadow-logging is only free when the counterfactual is
pure computation. The upgrade offer qualifies: deciding whether it would have fired needs
nothing but the quote already in hand, so it is always computed and logged. Flexible-date
alternatives do not — establishing what the agent *would* have offered means really
calling `/quote` for each candidate date. Pretending that costs nothing would be
dishonest, so that flag has three modes (`off` / `shadow` / `on`) instead of two, and
`shadow` is an explicit, deliberate choice to spend the extra API traffic to gather the
data that would justify turning it `on`. The log records `would_surface` and `max_saving`
— what the customer would have been shown and what they would have been offered — which
is the measurement the whole scheme exists to produce.

Point 3 is what makes this an experimentation surface rather than a config toggle. Avis can
measure the counterfactual — how often the feature would have fired, what it would have
offered, what the price deltas looked like — before ever exposing it to a real customer.
Half the battle here is A/B testing, including testing our own product assumptions.

**Interview framing:** "I made product assumptions I can't validate without your data, so I
made every one of them configurable, defaulted the aggressive ones off, and shadow-logged
them so you can measure the counterfactual before turning anything on. Here's the metric
I'd want to see for each."

### Flagged features

| Flag | Behavior | Default | Metric that would justify turning it on |
|---|---|---|---|
| `FLEXIBLE_DATE_ALTERNATIVES_MODE` | On extend, also quote nearby return dates and surface cheaper options. Three modes — `off` / `shadow` / `on` | `off` | Extension length delta; conversion rate; CSAT |
| `IN_FLOW_UPGRADE_OFFER` | Surface membership upgrade (standard → Avis Preferred) inside extend/modify where it would offset costs (e.g. late-fee exemption); acceptance escalates to a human with full flow context | Off | Offer→accept rate; complaint rate |
| `CANCEL_RETENTION_PROMPT` | Offer a date change once before processing a cancellation | Off | Save rate vs. abandonment/trust signals |

### Flexible-date alternatives (detail)

When a customer says "extend to Thursday," the agent concurrently quotes Thursday **and**
two nearby candidate dates, surfacing materially cheaper options.

**Why this isn't obviously revenue-negative.** The intuitive read is that showing cheaper
options costs Avis money. That's incomplete — anchoring cuts both ways. A customer who sees
"Thursday is $180, Friday is only $30 more" frequently extends *longer*, not shorter. The
honest position is that **the revenue effect is ambiguous and should be A/B tested** — which
is exactly why it's flagged and shadow-logged rather than assumed.

**Design constraints:**

- **Framed as flexibility, not correction.** The customer named Thursday for a reason — a
  flight, a meeting. "If your plans are flexible, here are two nearby options" respects
  that; implying they chose wrong does not.
- **Materiality threshold.** Only surface if the saving is meaningful. A $4 delta is noise
  and makes the agent feel chatty. Default: max($15, 10% of the primary quote's total),
  flag-configurable — shadow logs will calibrate it.
- **Two alternatives maximum.**
- **Never blocks the primary path.** Speculative quotes fan out concurrently; if they're
  slow or fail, they're dropped silently. The extend the customer actually asked for is
  never delayed by an optional nicety.
- **Rate limits / side effects — resolved.** `/quote` is documented as having no side
  effects (no inventory holds), and no rate limits are documented. The cost is reliability:
  the ~1% transient failure rate applies per call, which the "drop silently" rule absorbs.
  Defaulted **off** anyway — availability/quote paths are the flakiest in the API, and the
  behavior is commercially unvalidated.

---

## 5. Open questions — resolved from `docs/api-reference.md`

- [x] **Separate endpoints.** `extend` and `modify` are distinct; `modify` is a superset
      (pickup time, return time, return location). Both share `POST /quote`
      (`change_type: "extend" | "modify"`). "Modify reuses the Extend path" holds.
- [x] **Active vs upcoming.** No documented distinction; the only state-related error is
      `409 RESERVATION_NOT_ACTIVE`. Handle the 409, don't model a state machine the API
      doesn't expose.
- [x] **Quote.** Returns a full charges breakdown (daily rate, extension days, late fee,
      one-way fee, taxes, total). Not *required* before a write by the API — but our
      confirmation gate requires it. **No quote exists for cancel** (see handoff rules).
- [x] **Availability.** Not required for a time-only extend; it gates location-change
      modify (`409 VEHICLE_UNAVAILABLE` with alternatives) and powers the flexible-date
      feature. Confirms location-Modify as a stretch goal.
- [x] **Failure modes.** ~99% reliable with occasional 5xx and slow/timed-out responses,
      worst on availability and writes. Strict input validation (4xx envelope). No
      documented rate limits.
- [x] **Email mismatch.** `403 VERIFICATION_FAILED`, distinct from
      `404 RESERVATION_NOT_FOUND` — so not-leaking-which is on the agent's wording, not
      the API. Reads are fully open (name, card last-four, plate), so the agent limits
      what it echoes back before verification. New finding: **extend/modify writes also
      require `payment.cvv` + `payment.billing_zip`** — the confirmation gate includes a
      collect-payment-details step, plus `PAYMENT_VALIDATION_ERROR` / `PAYMENT_DECLINED`
      error paths. Cancel needs only email.
- [x] **Refund/penalty.** Computed by the API, but only *on the cancel call itself* — the
      pre-confirmation estimate comes from the KB, framed as an estimate; material
      divergence escalates (see §3).

### Edge cases to design around

- [x] **Idempotency on writes — solved.** Write endpoints accept an `Idempotency-Key`
      header that replays the original response on retry. Every write sends one; write
      retries become safe. (Writes are also ephemeral in this mock — nothing persists —
      so testing is free.)
- [ ] **Reservation state machine.** What states exist, and which transitions are legal?
      Extend on an already-returned rental, cancel on an already-cancelled one, modify
      mid-rental. The agent must read state before acting rather than trusting that the
      request makes sense.
- [ ] **Quote staleness.** If the customer deliberates for several minutes between quote
      and confirmation, is the quote still valid? If the API re-prices on write, the
      customer could be charged something they never agreed to — which defeats the
      confirmation gate. Check for a quote TTL; otherwise re-quote before write and surface
      any delta.
- [ ] **Timezones.** Rental datetimes are local to the pickup branch. "Extend until Friday
      5pm" is ambiguous across a UTC boundary and off-by-one-day errors are easy to write
      and embarrassing to demo.
- [ ] **Source-of-truth conflict.** If a knowledge-base article states a different
      cancellation penalty than the API returns, the **API wins** — it's the system of
      record; the KB is help-center prose that may be stale. Stated explicitly because an
      agent that quotes a RAG-sourced number and then charges something different is a
      trust failure.

---

## 6. Git workflow

- **Short-lived feature branches, merged to main quickly**: `feat/api-client`,
  `feat/extend`, `feat/cancel`, `feat/modify`, `feat/upgrade-upsell`. Each merges as soon
  as it works — the build is sequential by design (Cancel stress-tests Extend's
  abstractions), so long-lived parallel branches would only create conflicts in shared
  scaffolding.
- **Commit and push frequently** — small commits, so there's always a good point to fall
  back to if a feature fails, and the history tells the story of the build.
- **No worktrees** — they pay off for simultaneous work on multiple branches; this build
  is one person working sequentially. Revisit only if we parallelize (e.g. building
  Modify while reviewing Cancel).
- DECISIONS.md is updated whenever rationale changes, committed alongside the change.

---

## 7. Running it

❓ To be filled in during the build: setup steps, env vars, how to start the CLI, where
logs are written.

---

## 8. Changelog

- **2026-08-06** — Built Cancel (`src/policy.py`, `src/cancel_flow.py`), to the design in
  §3 with no deviations. The estimator is pure computation citing `kb_can_01` — no API
  call, nothing to time out — and the previously-unexecutable policy branches (pre-pickup
  >48h/≤48h, pay-at-counter, penalty capped at prepaid) ran for the first time under the
  fixture harness. Confirmation inherits every gate from Extend: staged estimate, turn
  boundary, single-use, verification lockout, `handed_off` blocking. The variance check is
  asymmetric per the §3 decision: adverse beyond $1 escalates as remediation (the write
  has already happened), favorable is reported as good news without burning a handoff.
  Verified live end-to-end: mid-rental "cancel" triggered disambiguation and an early
  return was answered from `kb_can_04` with nothing cancelled; an explicit cancellation
  completed with the estimate matching the actual to the cent. Retention prompt
  shadow-logs; three new gates mutation-verified; queue items #5 (bot identity in the
  greeting) and #6 (early-return answer) closed as side effects. 80+ tests, 7 files, green.
- **2026-08-06** — External audit against `main` (post-handoff-fix) found nine issues,
  independently reproduced before fixing, each with a regression test; the four new
  guards are mutation-verified. Highest severity: switching reservations mid-conversation
  leaked verification and a staged quote across bookings (§3, confirmation gate); terminal
  API failures set `escalated` but not the enforced `handed_off` flag, the same bug as the
  2026-08-05 incident reached through a different path; and a handoff test's stub was
  never reached because `agent.py` had imported a client function by bare name — the third
  instance of a test that doesn't test what it claims (§2, testing strategy). Also closed:
  quotes were chargeable twice (no consumed flag), nothing enforced a turn boundary
  between quote and charge (§3), an unpriced quote could still be confirmed, a stored
  unparseable datetime crashed past validation, and a threadpool fix for bounding
  speculative-quote latency silently didn't work on first attempt (`with` blocks on
  `wait=True` regardless of a per-future timeout) — caught by the timing test, not by
  inspection. 52 tests across 6 files green; live happy path re-verified end to end.
- **2026-08-05** — Built the knowledge base (`src/kb.py`) as designed: IDF-damped
  authority-ranked retrieval, legacy-article suppression, provenance logging. One
  deviation — added IDF weighting after plain term frequency let "return" swamp
  discriminating terms in longer queries. Built the fixture/test harness
  (`tests/fixtures.py`, `tests/test_client.py`) after probing already-merged flow code
  with malformed payloads found two live crashes (`KeyError` on a reservation missing
  `dates`, `TypeError` on a null return datetime); added `validate_reservation()` at the
  client boundary so malformed payloads become a graceful escalation instead of an
  exception. A second unscripted human session found the agent announcing a handoff and
  then answering the question itself 19 seconds later, and fabricating an access-control
  policy ("according to Avis policy...") that exists nowhere in the knowledge base and
  contradicts the agent's own instructions — see §3, handoff-to-human and the agent's
  remit. Both fixed structurally: `handed_off` as an enforced terminal state split into
  hard/assistive, and affirmative-permission instructions replacing a single-verb remit
  that left every other request to be answered by invention. A follow-up bug sweep before
  Cancel found and fixed a card-digit disclosure to an unverified caller and six of eight
  malformed customer datetimes escaping as raw `ValueError`.
- **2026-08-05** — Knowledge-base and cancel design settled ahead of building either.
  Retrieval will be lexical with authority/recency ranking rather than embeddings, and
  will never source a number that drives money — a deterministic policy function computes
  figures, the article is attached as citation. Retrieval provenance doubles as a stale-KB
  detector for Avis. Probed the API on cancel semantics and found that shortening a rental
  is rejected (`INVALID_CHANGE`) while lengthening succeeds, so no early-return path
  exists; "cancel" on an active rental therefore diverges from what most customers mean by
  it, and now branches on live reservation state rather than the customer's verb. Verified
  the penalty formula (`penalty == daily_rate`, `refund == prepaid − penalty`) across all
  six reservations, and catalogued the policy branches the test data cannot exercise.
- **2026-08-05** — `feat/extend` verified end-to-end and merged. Four scripted
  conversations against the live API and model: happy path, a customer demanding "don't
  quote me, just charge it" (refused, quoted anyway, nothing charged), out-of-scope
  cancel + upgrade (declined cleanly, escalated), and repeated wrong emails (escalated at
  the threshold). Two bugs the tests caught, both fixed in code rather than prompt:
  escalations were shipping an empty handoff payload when the agent escalated before
  looking up a reservation, and `SQLiteSession.clear_session()` is async — calling it
  synchronously silently leaked one customer's transcript into the next run. Added
  provider-side rate-limit retry: the LLM API throttles too, and a 429 after the customer
  has handed over card details is not an acceptable place to fail.
- **2026-08-04** — Built `feat/extend`. Confirmation gate moved into code
  (`extend_flow.commit_extension`) rather than prompt text; money logic separated from
  the agent so it is testable without an LLM. Found a deliberate contradiction in the
  knowledge base (grace period: 30 min official vs 2 hr legacy) — retrieval must rank on
  `authority`/`last_updated`, not similarity. Flexible-date flag became three-mode
  (`off`/`shadow`/`on`) after noticing its counterfactual is not free to compute.
  Verified against the live API: gate refuses un-quoted writes, verification escalates at
  threshold with full handoff payload, stale quotes re-price and block on any delta.
- **2026-08-04** — Read the API reference; resolved all ❓ items. Key findings: no cancel
  quote (KB estimate + variance-escalation rule added); extend/modify writes require
  CVV + billing ZIP (confirmation gate gains a payment step); `Idempotency-Key` supported
  (write retries safe); upgrade endpoint is a *membership* upgrade → repositioned as an
  in-flow upsell that escalates to a human with full flow context. Defaulted
  `FLEXIBLE_DATE_ALTERNATIVES` off. Added git workflow (short-lived feature branches,
  frequent pushes, no worktrees).
- **[pre-2026-08-04]** — Initial scoping. Chose Extend → Cancel → Modify; cut Upgrade and
  location-based Modify. Pre-dated reading the API reference.
