# Avis Agent — Take-Home

## Scenario

Avis Budget Group ("Avis") is one of the world's largest car rental companies. A large and growing share of their support volume isn't new bookings — it's customers **servicing an existing rental**: extending it, changing the time or location, upgrading, or cancelling. These are handled by human agents today.

Avis wants to know whether an AI agent can reliably handle a meaningful portion of that volume — freeing human agents for the harder cases — **without putting revenue or customer trust at risk.** They'd like to start with a pilot in a single market, and in these early days they'd rather handle a smaller set of cases well than stretch across everything.

Your job is to build a working prototype of that agent against Avis's (mock) systems.

## The servicing workflows

At a high level, the agent could help with several kinds of rental servicing:

- **Extend** — push out the return date/time on an active rental.
- **Modify** — change the pickup or drop-off time, or the location.
- **Upgrade** — move a customer from Standard to Preferred.
- **Cancel** — cancel a rental, applying any refund or penalty per policy.

The APIs support all of these (plus a `quote` to price a change and an `availability` check — see `docs/api-reference.md`). Each comes with its own upside and its own complications, for the business and for customers alike.

## What to build

Build a working prototype of the agent against Avis's (mock) systems, using the knowledge base and APIs provided. The design is yours — architecture, retrieval, and how you structure the agent are all up to you.

The decision we want you to make deliberately: **pick which workflow(s) to support** — extend, modify, upgrade, cancel. You likely won't have time for all of them, and that's fine. Decide where to focus, and be ready to walk us through **what you chose and why** so you can defend it. There's no single right answer.

Treat the API like a real production integration — read `docs/api-reference.md` closely for exactly how it behaves.

## What we're looking for

This is less about ticking off features than about how you think. We're assessing whether you can:

- **Reason about production systems** — build something that holds up against a real, imperfect API and the messiness of live customer requests.
- **Show product sense** — make smart calls about what to build, what to leave out, and what a genuinely good customer experience looks like.
- **Communicate and defend your decisions** — explain the tradeoffs you made and why, and stand behind your technical choices when we dig in.

Where you make an assumption, just call it out so we can follow your reasoning.

## What you have

- This repo — a sample starting point (a minimal scaffold, not a solution).
- `docs/api-reference.md` — the Avis API (base URL and your key are in `.env`).
- `data/knowledge-base/articles.json` — a set of Avis help-center articles.
- Your own LLM API key (in `.env`). We **highly recommend the OpenAI Agents SDK** — the starter scaffold is built on it — but you're welcome to use **Google's Agent Development Kit (ADK)** instead if you prefer.

### Test accounts

Use these to develop and demo. Each line is a reservation and the **email on file** you'll
need to authorize a change (in a real call the customer gives you the email; here it's
provided so you can test). Reads (`GET /reservations/{id}`) don't require the email — writes do.

| Reservation | Email on file |
|---|---|
| `AVS-29471835` | `sarah.johnson@example.com` |
| `AVS-48372915` | `marcus.lee@example.com` |
| `AVS-77001020` | `priya.patel@example.com` |
| `AVS-66002030` | `david.kim@example.com` |
| `AVS-50000001` | `robert.chen@example.com` |
| `AVS-99004050` | `tomas.rivera@example.com` |

Look each up to see its details, then exercise the workflows. (We'll also try your agent on
reservations you haven't seen, so don't hard-code to these.)

## Rules & scope

- **~5 hours.** This is intentionally more than you can finish — we care about your choices, not coverage.
- Use whatever AI tools you'd use on the job (Cursor, Claude Code, Codex, etc.). We'll ask you to explain your decisions.
- For the agent framework, we **highly recommend the OpenAI Agents SDK** (the scaffold uses it), but **Google's Agent Development Kit (ADK)** is also fine — your choice. Bring your own LLM API key.

## Be prepared to discuss

In the follow-up conversation we'll dig into your reasoning, not just your code. Come ready to talk through:

- **Scope** — which workflow(s) you chose, why, and what you intentionally left out.
- **Your design** — the key technical choices you made and the alternatives you weighed.
- **Production-readiness** — how your agent behaves when things go wrong, and how you'd operate and debug it.
- **Customer experience** — how the interaction feels, and where you'd draw the line on what the agent should and shouldn't do.

## Deliverables

- A **zip archive of this repo** (uploaded — not a GitHub link), with a README covering: your design decisions, which workflow(s) you chose and why, what you cut, how to run it, and where your logs are. Please exclude `.venv/`, `__pycache__/`, and any local secrets from the zip.

---

*This is a synthetic case built for evaluation. It does not reflect real Avis Budget Group systems, data, or policies.*
