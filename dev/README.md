# Local sandbox — synthetic reservations, no Avis API

A place to stress-test the agent against reservation states the six real test accounts
can't produce. **Nothing here is imported by `src/` or collected by pytest**; it exists
purely for hands-on local testing and can be deleted without touching the deliverable.

Why it exists: all six real accounts are `active`, prepaid, and months past their return
date, so they exercise exactly one branch of the policy (DECISIONS.md §3, "Situations the
test data does not cover"). There is no real account that is *genuinely, currently
active* — so the everyday case ("I have the car, it's due Sunday, can I keep it till
Tuesday") was never testable end-to-end against the real API at all.

## Use it

```bash
python dev/run_local.py                      # list scenarios
python dev/run_local.py active_mid_rental    # interactive, offline
python dev/run_local.py active_mid_rental --show   # print the payload
```

Needs `OPENAI_API_KEY` — the agent, prompt, and gates are all real, which is the point.
Only the Avis side is replaced. Everything logs to `logs/agent.jsonl` as usual.

## Scenarios

| Scenario | What it's for |
|---|---|
| `active_mid_rental` | **The everyday case.** Picked up 2 days ago, due back in 3. No late fee, nothing overdue. |
| `active_mid_rental_preferred` | Same, Avis Preferred — late-fee exemption path. |
| `pre_pickup_free` | Pickup in 5 days: cancellation with no penalty, full refund. |
| `pre_pickup_penalty` | Pickup in 20 hours: the ≤48h one-day-penalty branch. |
| `overdue_mid_rental` | Matches the real accounts' shape, for behavior parity. |
| `pay_at_counter` | `total_charged: 0` — "nothing to refund" (`kb_can_02`). |
| `inactive_cancelled` | `status: cancelled` — the `409 RESERVATION_NOT_ACTIVE` path. |

## Editing

`dev/sample_reservations/*.json` is plain, hand-editable JSON in the documented schema
(`docs/api-reference.md`). Change a date, a rate, a membership status, a status field —
rerun `dev/run_local.py` and the agent sees your edit. Dates are generated relative to
real wall-clock time; if they go stale, regenerate:

```bash
python dev/make_sample_reservations.py
```

### Forcing failures

Add `_inject_errors` to any reservation JSON. The next call to that operation raises
once, then clears:

```json
"_inject_errors": { "extend": "PAYMENT_DECLINED" }
```

Keys: `lookup`, `quote`, `extend`, `cancel`. Values: any code from the API reference's
error lists (`VERIFICATION_FAILED`, `RESERVATION_NOT_ACTIVE`, `PAYMENT_DECLINED`,
`VEHICLE_UNAVAILABLE`, …). `TIMEOUT`, `CONNECTION_ERROR`, or anything prefixed
`TRANSIENT_` / `HTTP_5` is treated as retryable so you can watch backoff behavior.

## Two honest caveats

**The pricing is reverse-engineered, not authoritative.** `daily_rate × days` (partial
days round up), a flat $29 late fee only when the return time has actually passed and the
member is standard, and 9.25% tax — the rate implied by every captured example. It
reproduces the *shape* faithfully enough to exercise conversations and gates; don't treat
the dollar figures as a prediction of what the real API will charge.

**Verification is permissive.** Any non-empty email verifies, because these payloads
carry no email (the real API doesn't return one either). Use `_inject_errors` with
`VERIFICATION_FAILED` to test that path deliberately.

## How it differs from `tests/fixtures.py`

Two deliberately separate seams, per DECISIONS.md §2:

- `tests/fixtures.py` — captured live payloads, minimally mutated, patched at the **HTTP
  transport** (`fake_transport`) to test `avis_client.py` itself. Fidelity rule: never
  invented from scratch.
- `dev/fake_backend.py` — synthetic payloads with computed-from-now dates, patched at the
  **client-function** seam to test the flows and the conversation. It has to invent,
  because the branch it's built for is one no captured payload can reach.

`patch_all()` rebinds all four references the agent actually calls, including
`extend_flow`'s and `cancel_flow`'s bare-imported names — patching only `avis_client`'s
module attributes would silently miss them, the exact bug DECISIONS.md §2 records
catching twice in the test suite.
