"""Generate synthetic reservation payloads for local, offline stress testing.

Not test fixtures in the tests/fixtures.py sense — those are captured live payloads,
minimally mutated, per the project's testing-fidelity rule (DECISIONS.md §2). These
are the opposite by necessity: dates computed relative to *real* wall-clock time, so
that "an active mid-rental reservation" is still actually mid-rental whenever this is
run. All six real test accounts are permanently overdue (both pickup and return in the
past) — there is no real account that is genuinely, currently active, which is exactly
the gap this fills.

Regenerate whenever the dates go stale (they won't during one sitting, but will after
enough days pass):

    python dev/make_sample_reservations.py

Each file is plain, hand-editable JSON matching the documented schema
(docs/api-reference.md). An optional top-level "_inject_errors" object lets you force a
specific client operation to fail once — see dev/fake_backend.py and dev/README.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

OUT_DIR = Path(__file__).resolve().parent / "sample_reservations"

# Same branch, same offset convention as the real captured fixtures: local to the
# pickup branch, not UTC. LAX = America/Los_Angeles, ORD = America/Chicago.
LAX = ZoneInfo("America/Los_Angeles")
ORD = ZoneInfo("America/Chicago")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _base(
    *,
    reservation_id: str,
    customer_id: str,
    customer_name: str,
    membership_status: str,
    vehicle_type: str,
    description: str,
    make_model: str,
    color: str,
    plate: str,
    location_code: str,
    location_name: str,
    address: str,
    pickup: datetime,
    ret: datetime,
    daily_rate: float,
    card_type: str,
    last_four: str,
    total_charged: float,
    status: str = "active",
) -> dict:
    return {
        "reservation_id": reservation_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "membership_status": membership_status,
        "vehicle": {"type": vehicle_type, "description": description,
                    "make_model": make_model, "color": color, "license_plate": plate},
        "pickup_location": {"code": location_code, "name": location_name, "address": address},
        "return_location": {"code": location_code, "name": location_name, "address": address},
        "dates": {"pickup_datetime": _iso(pickup), "current_return_datetime": _iso(ret),
                  "original_return_datetime": _iso(ret)},
        "pricing": {"daily_rate": daily_rate, "currency": "USD"},
        "payment": {"card_on_file": {"type": card_type, "last_four": last_four},
                    "total_charged": total_charged},
        "status": status,
    }


def build() -> dict[str, dict]:
    now_lax = datetime.now(LAX)
    now_ord = datetime.now(ORD)

    reservations: dict[str, dict] = {}

    # --- The one explicitly asked for: genuinely active, sensical dates ------------
    # Picked up 2 days ago, due back in 3 days. Nothing about this reservation is
    # overdue — unlike every real test account, which is why REVIEW_QUEUE-class bugs
    # (stale-date extension targets, grace-period confusion) never got exercised
    # against a truly active rental before now.
    reservations["active_mid_rental"] = _base(
        reservation_id="AVS-90000001", customer_id="CUST-900001",
        customer_name="Jordan Reyes", membership_status="standard",
        vehicle_type="midsize_sedan", description="Chevrolet Malibu or similar",
        make_model="2025 Chevrolet Malibu", color="Silver", plate="7XYZ890",
        location_code="LAX", location_name="Los Angeles International Airport",
        address="9217 Airport Blvd, Los Angeles, CA 90045",
        pickup=now_lax - timedelta(days=2), ret=now_lax + timedelta(days=3),
        daily_rate=45.99, card_type="Visa", last_four="4832",
        total_charged=round(45.99 * 5, 2),
    )

    # Same shape, Preferred member — exercises the late-fee waiver path on a rental
    # that will never actually incur a late fee at this rate, since it isn't overdue.
    reservations["active_mid_rental_preferred"] = _base(
        reservation_id="AVS-90000002", customer_id="CUST-900002",
        customer_name="Priya Nair", membership_status="avis_preferred",
        vehicle_type="suv", description="Toyota Highlander or similar",
        make_model="2025 Toyota Highlander", color="Black", plate="3LMN456",
        location_code="ORD", location_name="O'Hare International Airport",
        address="10000 W Balmoral Ave, Chicago, IL 60666",
        pickup=now_ord - timedelta(days=1), ret=now_ord + timedelta(days=4),
        daily_rate=64.99, card_type="Mastercard", last_four="1190",
        total_charged=round(64.99 * 5, 2),
    )

    # --- Pre-pickup: the branch no real account can reach (§3, "Situations the test
    # data does not cover") -----------------------------------------------------------
    reservations["pre_pickup_free"] = _base(
        reservation_id="AVS-90000003", customer_id="CUST-900003",
        customer_name="Alex Kim", membership_status="standard",
        vehicle_type="compact", description="Toyota Corolla or similar",
        make_model="2024 Toyota Corolla", color="White", plate="9DEF123",
        location_code="ORD", location_name="O'Hare International Airport",
        address="10000 W Balmoral Ave, Chicago, IL 60666",
        pickup=now_ord + timedelta(days=5), ret=now_ord + timedelta(days=8),
        daily_rate=38.99, card_type="Visa", last_four="2210",
        total_charged=round(38.99 * 3, 2),
    )

    reservations["pre_pickup_penalty"] = _base(
        reservation_id="AVS-90000004", customer_id="CUST-900004",
        customer_name="Morgan Blake", membership_status="standard",
        vehicle_type="fullsize_sedan", description="Nissan Altima or similar",
        make_model="2025 Nissan Altima", color="Gray", plate="5GHI789",
        location_code="LAX", location_name="Los Angeles International Airport",
        address="9217 Airport Blvd, Los Angeles, CA 90045",
        pickup=now_lax + timedelta(hours=20), ret=now_lax + timedelta(days=3, hours=20),
        daily_rate=41.99, card_type="Visa", last_four="6601",
        total_charged=round(41.99 * 3, 2),
    )

    # --- Overdue mid-rental, matching the shape of the six real test accounts, so it
    # can be diffed against them for behavior parity ---------------------------------
    reservations["overdue_mid_rental"] = _base(
        reservation_id="AVS-90000005", customer_id="CUST-900005",
        customer_name="Sam Okafor", membership_status="standard",
        vehicle_type="compact", description="Toyota Corolla or similar",
        make_model="2024 Toyota Corolla", color="Blue", plate="1JKL234",
        location_code="ORD", location_name="O'Hare International Airport",
        address="10000 W Balmoral Ave, Chicago, IL 60666",
        pickup=now_ord - timedelta(days=10), ret=now_ord - timedelta(days=4),
        daily_rate=38.99, card_type="Mastercard", last_four="7734",
        total_charged=round(38.99 * 6, 2),
    )

    # --- Pay-at-counter: nothing prepaid, kb_can_02's "nothing to refund" branch ----
    reservations["pay_at_counter"] = _base(
        reservation_id="AVS-90000006", customer_id="CUST-900006",
        customer_name="Dana Whitfield", membership_status="standard",
        vehicle_type="suv", description="Ford Explorer or similar",
        make_model="2025 Ford Explorer", color="Red", plate="8MNO567",
        location_code="LAX", location_name="Los Angeles International Airport",
        address="9217 Airport Blvd, Los Angeles, CA 90045",
        pickup=now_lax + timedelta(days=6), ret=now_lax + timedelta(days=9),
        daily_rate=58.99, card_type="Visa", last_four="3345",
        total_charged=0.0,
    )

    # --- Already cancelled: the 409 RESERVATION_NOT_ACTIVE branch -------------------
    reservations["inactive_cancelled"] = _base(
        reservation_id="AVS-90000007", customer_id="CUST-900007",
        customer_name="Taylor Nguyen", membership_status="standard",
        vehicle_type="compact", description="Toyota Corolla or similar",
        make_model="2024 Toyota Corolla", color="Black", plate="2PQR890",
        location_code="ORD", location_name="O'Hare International Airport",
        address="10000 W Balmoral Ave, Chicago, IL 60666",
        pickup=now_ord - timedelta(days=1), ret=now_ord + timedelta(days=2),
        daily_rate=38.99, card_type="Visa", last_four="5567",
        total_charged=round(38.99 * 3, 2), status="cancelled",
    )

    return reservations


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in build().items():
        path = OUT_DIR / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"  wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    print(f"\n{len(build())} reservations regenerated with dates relative to "
          f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}.")


if __name__ == "__main__":
    main()
