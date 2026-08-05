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
