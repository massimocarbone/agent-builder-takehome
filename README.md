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

## Then build

Head to [`BRIEF.md`](BRIEF.md). Build the RAG foundation and escalation logic first, then
choose which workflow(s) to support and why. The design is yours — `agent.py` and
`avis_client.py` just prove the wiring works; replace and extend them however you like.

When you submit, please update this README to cover: your design decisions, which
workflow(s) you chose and why, what you cut, how to run your code, and where your logs are.
Submit as a **zip archive of this repo** (uploaded, not a GitHub link) — exclude `.venv/`,
`__pycache__/`, and any local secrets.
