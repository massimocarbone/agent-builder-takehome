"""Run the real agent against a local synthetic reservation. No Avis API calls.

    python dev/run_local.py                          # list scenarios
    python dev/run_local.py active_mid_rental        # interactive CLI, offline
    python dev/run_local.py active_mid_rental --show # print the payload and exit

Still needs OPENAI_API_KEY — the agent and prompt are real, which is the point. Only
the Avis side is replaced (dev/fake_backend.py). Edit the JSON in
dev/sample_reservations/ between runs to test whatever you think of; add
``"_inject_errors": {"extend": "PAYMENT_DECLINED"}`` to force a failure path.

Everything logs to logs/agent.jsonl as usual, so retrieval provenance, gate refusals,
and escalations are all inspectable afterwards exactly as in a live run.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

DEV = Path(__file__).resolve().parent
ROOT = DEV.parent
SAMPLES = DEV / "sample_reservations"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(DEV))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


def _scenarios() -> dict[str, Path]:
    return {p.stem: p for p in sorted(SAMPLES.glob("*.json"))}


def _describe(payload: dict) -> str:
    dates = payload["dates"]
    return (f"{payload['reservation_id']}  {payload['customer_name']:<16} "
            f"{payload['membership_status']:<15} status={payload['status']:<10} "
            f"pickup={dates['pickup_datetime'][:16]}  return={dates['current_return_datetime'][:16]}")


def main() -> int:
    scenarios = _scenarios()
    if not scenarios:
        print("No scenarios. Run: python dev/make_sample_reservations.py")
        return 1

    if len(sys.argv) < 2:
        print("Local scenarios (dev/sample_reservations/*.json):\n")
        for name, path in scenarios.items():
            print(f"  {name:<28} {_describe(json.loads(path.read_text()))}")
        print("\nUsage: python dev/run_local.py <scenario> [--show]")
        print("Dates stale? Regenerate: python dev/make_sample_reservations.py")
        return 0

    name = sys.argv[1]
    if name not in scenarios:
        print(f"Unknown scenario {name!r}. Available: {', '.join(scenarios)}")
        return 1

    payload = json.loads(scenarios[name].read_text())
    if "--show" in sys.argv:
        print(json.dumps(payload, indent=2))
        return 0

    import agent  # noqa: E402  (after load_dotenv, like agent.py's own __main__)
    from agents import SQLiteSession  # noqa: E402
    from fake_backend import FakeBackend, patch_all, unpatch  # noqa: E402
    from session import ServicingSession  # noqa: E402

    backend = FakeBackend(payload)
    originals = patch_all(backend)

    servicing_session = ServicingSession()
    history = SQLiteSession(f"avis-local-{uuid.uuid4()}")

    print(f"LOCAL SANDBOX — no Avis API calls. Scenario: {name}")
    print(f"  {_describe(payload)}")
    if backend._pending_errors:
        print(f"  injected errors pending: {backend._pending_errors}")
    print("  Any non-empty email verifies here. Ctrl-C or 'quit' to exit.\n")
    print("Agent: Hi! I'm Avis's automated assistant — I can help extend or cancel a "
          "reservation, or answer questions about one. Do you have your reservation number handy?")

    try:
        while True:
            try:
                user_input = input("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return 0
            if user_input.lower() in {"quit", "exit"}:
                return 0
            if not user_input:
                continue
            try:
                result = agent.run_turn(user_input, servicing_session, history)
                print(f"\nAgent: {result.final_output}")
            except Exception as exc:  # noqa: BLE001 - sandbox must not die mid-conversation
                print(f"\n[sandbox] {type(exc).__name__}: {exc}")
    finally:
        unpatch(originals)


if __name__ == "__main__":
    raise SystemExit(main())
