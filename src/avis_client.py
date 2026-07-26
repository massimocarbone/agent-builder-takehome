"""A minimal Avis API client — ONE worked example so you don't have to
reverse-engineer the plumbing. Extend or replace it however you like.

Run directly to verify your .env is set up and the API is reachable:
    python src/avis_client.py
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv()

AVIS_API_URL = os.environ["AVIS_API_URL"].rstrip("/")
AVIS_API_KEY = os.environ["AVIS_API_KEY"]


def get_reservation(reservation_id: str) -> dict:
    """Look up a reservation by id. Raises requests.HTTPError on a non-2xx response.

    Note: the API is production-like — it can return transient 5xx errors or be
    slow. This example does NOT handle that; building in sensible retries,
    timeouts, and error handling is part of the exercise.
    """
    resp = requests.get(
        f"{AVIS_API_URL}/reservations/{reservation_id}",
        headers={"X-API-Key": AVIS_API_KEY},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


if __name__ == "__main__":
    reservation = get_reservation("AVS-29471835")
    print("Connected. Sample reservation:")
    print(f"  {reservation['customer_name']} — {reservation['vehicle']['description']}")
    print(f"  returns {reservation['dates']['current_return_datetime']} at {reservation['return_location']['code']}")
