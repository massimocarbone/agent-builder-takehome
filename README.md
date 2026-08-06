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
   `AVIS_API_URL` is already filled in. Add your **`AVIS_API_KEY`** — grab it from this 1Password
   link: https://share.1password.com/s#yaM3mKXX9P8_BHxWxpaSzhqqe4YIq4p6AaUafkLJ9vY — plus your own
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

## Then build

Head to [`BRIEF.md`](BRIEF.md). Build the RAG foundation and escalation logic first, then
choose which workflow(s) to support and why. The design is yours — `agent.py` and
`avis_client.py` just prove the wiring works; replace and extend them however you like.

When you submit, please update this README to cover: your design decisions, which
workflow(s) you chose and why, what you cut, how to run your code, and where your logs are.
Submit as a **zip archive of this repo** (uploaded, not a GitHub link) — exclude `.venv/`,
`__pycache__/`, and any local secrets.
