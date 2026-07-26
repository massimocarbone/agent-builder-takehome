"""A runnable hello-world agent built on the OpenAI Agents SDK (recommended).

This exists ONLY to prove your environment works and to show the wiring. It is
NOT a solution: it does not implement RAG over the knowledge base, and it does
not implement any of the extend / modify / upgrade / cancel workflows. That's
your job — see BRIEF.md.

The OpenAI Agents SDK is recommended, not required — if you'd rather use Google's
Agent Development Kit (ADK), you're welcome to replace this scaffold entirely.

Run it:
    python src/agent.py
"""
import os

from dotenv import load_dotenv

load_dotenv()

from agents import Agent, Runner, function_tool  # noqa: E402

from avis_client import get_reservation  # noqa: E402


@function_tool
def lookup_reservation(reservation_id: str) -> dict:
    """Look up an Avis reservation by its ID (e.g. 'AVS-29471835')."""
    return get_reservation(reservation_id)


# TODO (you): add a real RAG tool over data/knowledge-base/articles.json.
@function_tool
def search_knowledge_base(query: str) -> str:
    """Stub. Replace with retrieval over the Avis help-center articles."""
    return "Knowledge base search is not implemented yet — see BRIEF.md."


agent = Agent(
    name="Avis Assistant",
    instructions=(
        "You are a helpful Avis rental support agent. You can look up reservations. "
        "This is only a starter skeleton — extend it to do real work."
    ),
    tools=[lookup_reservation, search_knowledge_base],
)


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY in your .env first (see env.example).")
        return
    result = Runner.run_sync(
        agent,
        "Look up reservation AVS-29471835 and tell me when it's due back.",
    )
    print(result.final_output)


if __name__ == "__main__":
    main()
