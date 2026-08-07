# Avis Agent — Take-Home

A starting point for the Avis rental-servicing agent take-home. **Start with [`BRIEF.md`](BRIEF.md)** — it describes the task. This repo is a thin scaffold to save you setup time; it is **not** a solution.

## What's here

```
.
├── BRIEF.md                          # the task — read this first
├── docs/
│   └── api-reference.md              # the Avis API your agent calls
├── data/
│   └── knowledge-base/articles.json  # Avis help-center articles (for RAG)
├── src/
│   ├── agent.py                      # a runnable hello-world agent (NOT a solution)
│   └── avis_client.py                # one worked example call to the Avis API
├── env.example
└── requirements.txt
```

## Setup

1. **Python 3.10+ required** (the OpenAI Agents SDK needs it). Check first — on macOS the
   system `python3` is often 3.9, which won't work:
   ```bash
   python3 --version            # need 3.10 or higher
   # if it's < 3.10, install a newer one (e.g. `brew install python@3.12`) and use that:
   python3 -m venv .venv && source .venv/bin/activate
   ```
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure your environment:**
   ```bash
   cp env.example .env
   ```
   `AVIS_API_URL` is already filled in. Obtain your **`AVIS_API_KEY`** through the assignment's
   private credential channel, then add it locally alongside your own
   LLM key: we **highly recommend the OpenAI Agents SDK** (set `OPENAI_API_KEY`) — the scaffold
   uses it — but you may use **Google's ADK** instead (set `GOOGLE_API_KEY` and adapt the scaffold).
4. **Verify the API connection** (looks up a sample reservation):
   ```bash
   python src/avis_client.py
   ```
5. **Run the starter agent:**
   ```bash
   python src/agent.py
   ```

## Tests

Run the complete test suite with:

```bash
python -m pytest
```

Four tests report as `XFAIL`. Each is a safeguard we **decided not to build**, not one
that's merely outstanding — the reason is attached to the test and argued in
[`DECISIONS.md`](DECISIONS.md) (§2 retrieval limits, §3 "Gaps left open on purpose").
Strict mode means implementing one turns its unexpected pass into a failing result, so a
gap can't quietly half-close; the marker comes off and the test graduates into the normal
suite.

`xfail` also swallows *errors*, so a test that crashes before reaching its assertion looks
identical to a documented gap. Before believing one, read the real failure:

```bash
python -m pytest tests/test_additional_safety.py --runxfail
```

Run one file or one area while developing with, for example:

```bash
python -m pytest tests/test_cancel_flow.py
python -m pytest -k cancellation
```

## Knowledge-base retrieval modes

Policy retrieval is deterministic lexical scoring by default. `KB_RETRIEVAL_MODE`
(env var, see [DECISIONS.md](DECISIONS.md) §2 and the §4 flag table) selects the
candidate producer:

- `lexical` *(default)* — deterministic scoring only; no model call on the policy path.
- `shadow` — the librarian classification call also runs and every attempt (including
  failures) is logged to `logs/agent.jsonl` as `kb_retrieval_comparison`; lexical is
  still what the customer gets. This is the data-gathering mode.
- `librarian` — the librarian serves, falling back to lexical on any failure. Its picks
  still pass the same deterministic finalizer (authority suppression, verbatim corpus
  content), so it proposes articles but never adjudicates authority or invents text.

The librarian's retrieval quality is measured by a scored eval that pytest deliberately
does not collect (quality is probabilistic; it must not gate the build):

```bash
python tests/eval_librarian.py
```

### Isolated local test artifacts

For a production-like local run, use the wrapper instead of invoking pytest directly:

```bash
python scripts/run_tests.py
python scripts/run_tests.py tests/test_client.py -k retry
```

Each invocation creates `artifacts/test-runs/<run-id>/` containing `metadata.json`,
`summary.json`, `console.log`, `pytest.log`, `junit.xml`, `agent.jsonl`, and `api.jsonl`.
The directory is git-ignored and one run never appends to another. The wrapper supplies
fake Avis credentials only to the test subprocess (all HTTP is scripted); production
imports still fail fast when real configuration is absent.

Decision and API JSONL events carry available `run_id`, `test_id`, `conversation_id`,
`turn_id`, and `operation_id` fields. One operation ID spans all retries of a request,
while write idempotency keys keep their existing retry-safety behavior. Known email,
card, CVV, billing-ZIP, authorization, and API-key values are recursively redacted. The
run summary also scans the structured JSONL artifacts for obvious raw email/card/CVV
patterns and returns a non-zero status if it finds one. Treat this as a guardrail, not a
replacement for access control or a dedicated secret scanner.

### Generative safety tests

Hypothesis broadens the deterministic safety suite beyond the named examples:

```bash
python -m pytest tests/test_properties.py tests/test_state_machine.py
```

The property tests minimally mutate captured reservation fixtures around validation,
datetime, pricing, cancellation, retry, and idempotency boundaries. The rule-based state
machine explores interleaved reservation switches, quotes, confirmations, repricing,
cancellation disambiguation, and hard handoffs. Both use bounded local settings and
scripted transports, so they do not call the live Avis API or an LLM. To reproduce an
exact stateful run, pass a seed such as `--hypothesis-seed=20260806`.

## Design & Scope

**Workflows chosen: Extend and Cancel.** Modify was cut — it doesn't route money through
the system (customers call the branch to change a non-rate-affecting detail), and adding
it would duplicate the reservation-lookup and escalation scaffolding without testing those
mechanisms further. See [DECISIONS.md](DECISIONS.md) §1 for the full reasoning.

**Architecture: three deterministic layers wrapping an LLM.**

1. **KB retrieval** (deterministic, testable without a model). Lexical scoring with
   authority-aware ranking. The corpus contains a legacy article with a stale grace period;
   deterministic scoring catches that in regression tests. See §2 for the homonym problem
   the lexical boundary leaves unsolved, and `KB_RETRIEVAL_MODE` (above) for the librarian
   mitigation.
2. **Money flow** (plain Python). Extend and Cancel both estimate a price, gate a
   confirmation on it, write if approved, then compare actual vs. estimated (the variance
   check). The agent proposes actions; deterministic code enforces them. Write idempotency
   is guaranteed by an Avis API idempotency key stable across retries, plus a
   once-per-request `consumed` flag. See §3 for the full confirmation gate semantics and
   the early-return-vs-cancel disambiguation that only lives in data, not in the agent's
   decision logic.
3. **Escalation** (structured state, enforced at tool entry). A hard handoff (`handed_off=True`)
   blocks all action tools. Everything the customer said, every quote, every verification
   attempt travels with the escalation, so the human never has to re-interview. §3 documents
   the assistive vs. hard distinction.

**Rules that safety depends on.**

- **Confirmation gates.** A write refuses unless a quote the customer actually *saw* lives
  in the session state. The model cannot quote and confirm in the same turn — the gate
  compares conversation turns, not wall time. Single-use (a retried tool call or replayed
  turn cannot charge twice).
- **Authority-aware suppression.** Legacy articles are dropped when a higher-authority
  version exists in the same category, computed from the corpus unconditionally, not from
  what matched this query (the fix for the "precise retriever" problem).
- **Escalation finality.** A hard handoff sets a terminal flag checked at every tool entry.
  The model going quiet is enforced in code, not requested in the prompt.
- **Secrets redaction.** Email, card, CVV, ZIP: redacted at log time, never in plaintext
  to a human, never visible in test artifacts (which are sandboxed and git-ignored). Best
  effort — free text can always hide a number somewhere this misses.

See [DECISIONS.md](DECISIONS.md) in full for design trade-offs, cross-cutting rules (§3),
feature-flag guidance (§4), the API surface and its gaps (§5), and the pre-submission testing
phase results (§8).

## Logs & Observability

Decision and API events log to `logs/agent.jsonl` and `logs/api.jsonl` respectively.
Every event carries a conversation ID (stable across a customer interaction), operation ID
(spans retries of one logical call), and run ID (from the test harness). Structured fields
allow aggregating by reservation, error code, escalation reason, policy branch, and KB
retrieval source/accuracy.

**From a live run:** `python src/agent.py` writes to these logs as the customer talks.

**From tests:** The wrapper script (see "Isolated local test artifacts" above) isolates each
run's artifacts. The `summary.json` in each run's directory includes a full event dump, a
pass/fail verdict, and a scan for accidentally logged secrets (returns nonzero if any found).

Logs are the source of truth for what the agent decided and why. The transcript is included
in escalation payloads to the human but never surfaced to the customer (answers come through
the agent's own text, not logged reasons).

## Running the agent

```bash
python src/agent.py
```

Requires `.env` configured (see Setup above), with at minimum `AVIS_API_KEY`,
`OPENAI_API_KEY` (or `GOOGLE_API_KEY`), and optional feature-flag toggles (see
[DECISIONS.md](DECISIONS.md) §4). The agent runs one conversation per invocation; see
`src/agent.py` for the Runner integration if you want to host it as a service.

## Then build (if extending)

Head to [`BRIEF.md`](BRIEF.md) if this is your starting point. This submission is complete
and tested (120 passed, 4 deliberate xfail — see Tests above). The design is stable; extend
it by adding workflows, improving KB retrieval, or adding new gates. `src/agent.py` and
`src/avis_client.py` are the integration points. Replace and extend as needed.
