# Avis Agent — Take-Home

This is a working prototype for servicing an existing Avis rental. It supports **Extend**
and **Cancel**, with deterministic API integration, pricing/policy logic, confirmation
gates, and structured escalation around an LLM conversation layer.

Start with [`BRIEF.md`](BRIEF.md) for the evaluation goals, then see
[`DECISIONS.md`](DECISIONS.md) for scope, rationale, known limits, and testing evidence.

## Setup

1. **Python 3.10+ required** (the OpenAI Agents SDK needs it):
   ```bash
   python3 --version
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. **Install dependencies:**
   ```bash
   python -m pip install -r requirements.txt
   ```
3. **Configure your environment:**
   ```bash
   cp env.example .env
   ```
   `AVIS_API_URL` is already filled in. Obtain your **`AVIS_API_KEY`** through the assignment's
   private credential channel, then add it locally alongside your own **`OPENAI_API_KEY`**.
   This submitted implementation uses the OpenAI Agents SDK; Google ADK would require a
   separate adaptation.
4. **Verify the API connection** (looks up a sample reservation):
   ```bash
   python src/avis_client.py
   ```
5. **Run the agent:**
   ```bash
   python src/agent.py
   ```

## Tests

Run the complete test suite with:

```bash
python -m pytest
```

Four tests report as `XFAIL`. They document known residual risks rather than passing
coverage; their reasons are attached to the tests and discussed in
[`DECISIONS.md`](DECISIONS.md). The final independent review also found additional risks
recorded in §8B.2. Strict mode means an unexpected pass fails until the marker is removed
and the test graduates into the normal suite.

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
inert default Avis settings for the test subprocess and all HTTP is scripted; production
imports still fail fast when real configuration is absent.

Decision and API JSONL events carry available `run_id`, `test_id`, `conversation_id`,
`turn_id`, and `operation_id` fields. One operation ID spans all retries of a request.
Structured secret fields and obvious labelled/card-like free-text patterns are redacted;
the run summary scans structured artifacts for obvious email/card/CVV patterns and returns
a non-zero status if it finds one. Bare free-text CVVs and ZIPs remain a documented
limitation (§8B.2), so this is a guardrail—not a replacement for access control or a
dedicated secret scanner.

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

**Supported: Extend and Cancel.** **Cut: Modify and Upgrade.** We prioritized complete,
testable coverage of two distinct risk classes—an extension that can charge and an
irreversible cancellation—over a rushed third workflow. Modify remains a natural future
addition because it can reuse reservation lookup and escalation scaffolding, but it also
requires payment verification and can incur one-way fees. See [DECISIONS.md](DECISIONS.md)
§1 for the full rationale.

**Architecture: three deterministic layers wrapping an LLM.**

1. **KB retrieval** (deterministic, testable without a model). Lexical scoring with
   authority-aware ranking. The corpus contains a legacy article with a stale grace period;
   deterministic scoring catches that in regression tests. See §2 for the homonym problem
   the lexical boundary leaves unsolved, and `KB_RETRIEVAL_MODE` (above) for the librarian
   mitigation.
2. **Money flow** (plain Python). Extend and Cancel stage a price or estimate, require a
   later customer turn before the write, and reconcile the actual result afterward. The
   agent proposes actions; deterministic code enforces the normal flow. The residual
   concurrency, consent, and stale-state limits are documented in §8B.2.
3. **Escalation** (structured state). Normal hard-handoff paths set `handed_off=True` and
   block reservation-scoped tools. Handoff context includes the interaction needed for a
   human to continue; the final review records a remaining downgrade edge case in §8B.2.

**Rules that safety depends on.**

- **Confirmation gates.** A write requires a staged price/estimate from an earlier turn;
  the model cannot quote and confirm in the same turn. This is a meaningful deterministic
  control, not proof of affirmative consent or concurrency safety—see §8B.2.
- **Authority-aware suppression.** Legacy articles are dropped when a higher-authority
  version exists in the same category, computed from the corpus unconditionally, not from
  what matched this query (the fix for the "precise retriever" problem).
- **Escalation finality.** A hard handoff is enforced on normal action-tool paths; the
  remaining downgrade edge case is documented in §8B.2.
- **Secrets redaction.** Structured and labelled card/CVV fields are redacted in logs;
  free-text redaction is best effort. A bare CVV or ZIP can survive the transcript path,
  so do not treat local artifacts or handoffs as a compliant secret store (§8B.2).

See [DECISIONS.md](DECISIONS.md) in full for design trade-offs, cross-cutting rules (§3),
feature-flag guidance (§4), the API surface and its gaps (§5), and the pre-submission testing
phase results (§8).

## Logs & Observability

Decision and API events log to `logs/agent.jsonl` and `logs/api.jsonl` respectively.
Events carry the correlation IDs applicable to their lifecycle: conversation/turn IDs for
agent work, operation IDs for API calls, and run/test IDs in the test harness. Structured
fields allow aggregating by reservation, error code, escalation reason, policy branch, and
KB retrieval source/accuracy.

**From a live run:** `python src/agent.py` writes to these logs as the customer talks.

**From tests:** The wrapper script (see "Isolated local test artifacts" above) isolates each
run's artifacts. `summary.json` records run metadata, pytest/final exit status, duration,
and the artifact-secret scan; the separate JSONL files hold the events.

Logs are the source of truth for what the agent decided and why. The transcript is included
in escalation payloads to the human but never surfaced to the customer (answers come through
the agent's own text, not logged reasons).

## Running the agent

```bash
python src/agent.py
```

Requires `.env` configured (see Setup above), with at minimum `AVIS_API_KEY` and
`OPENAI_API_KEY`; optional feature flags are in [DECISIONS.md](DECISIONS.md) §4. The agent
runs one conversation per invocation; see `src/agent.py` for the Runner integration if you
want to host it as a service.

## Create the submission archive

After committing final tracked documentation changes, run:

```bash
python scripts/package_submission.py
```

Upload `dist/avis-servicing-agent-submission.zip`. The script archives committed tracked
files only and refuses a dirty tracked worktree, which excludes local `.env`, `.venv`,
`__pycache__`, and logs. Do not create a recursive Finder/desktop zip of the working
directory.

## Extending after submission

This prototype is complete for the chosen scope and tested with `120 passed, 4 documented
XFAILs`. It also has explicitly recorded residual risks; see [DECISIONS.md](DECISIONS.md)
§8B.2 before expanding the workflows, retrieval, or money-moving gates.
