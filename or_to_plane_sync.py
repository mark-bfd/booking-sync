"""
OwnerRez to Plane sync — pulls current OR bookings, creates Plane prep tickets.

Idempotent: checks Plane for an existing ticket referencing the OR booking ID
in the description; skips if found, creates if not.

Use cases:
- Backfill: run once to create tickets for bookings already in OR
- Periodic: cron-schedule to catch bookings that webhooks may have missed

Creates two ticket types per booking:
- Prep ticket: target_date = arrival - 1 day, OPERATIONS module
- (future) Review ticket: created when departure is in past, target_date = departure + 2 days

Configuration (environment variables):
    OWNERREZ_USERNAME     OwnerRez account email that owns the API token
    OWNERREZ_API_KEY      OwnerRez personal access token
    OWNERREZ_PROPERTY_ID  numeric property ID to sync
    (plus the PLANE_* variables — see plane_issue.py)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from base64 import b64encode
from datetime import date, timedelta

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from plane_issue import ISSUE_PREFIX, PlaneClient, STATE_TODO  # noqa: E402

OR_BASE = "https://api.ownerrez.com/v2"
# OwnerRez v2 auth is HTTP Basic: username = the account email that OWNS the
# personal access token (a PAT issued under one login returns 401 for others).
OR_USERNAME = os.environ.get("OWNERREZ_USERNAME", "user@example.com")
OR_PROPERTY_ID = int(os.environ.get("OWNERREZ_PROPERTY_ID", "0"))


def or_request(path: str, params: dict | None = None) -> dict:
    token = os.environ["OWNERREZ_API_KEY"]
    auth = b64encode(f"{OR_USERNAME}:{token}".encode()).decode()
    url = f"{OR_BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def parse_guest_name(booking: dict) -> str:
    """OR iCal-imported bookings have guest name in notes 'iCal Title: Reserved - <name>'."""
    notes = booking.get("notes", "") or ""
    if "iCal Title:" in notes:
        line = [l for l in notes.split("\n") if "Reserved" in l]
        if line:
            return line[0].split("-", 1)[-1].strip()
    # Native bookings will have first_name/last_name on guest object
    return f"Guest #{booking.get('guest_id', '?')}"


def days_until(target: date) -> int:
    return (target - date.today()).days


def upsert_prep_ticket(plane: PlaneClient, booking: dict, existing_tickets: list[dict]) -> str:
    booking_id = booking["id"]
    arrival = date.fromisoformat(booking["arrival"])
    departure = date.fromisoformat(booking["departure"])
    nights = (departure - arrival).days
    guest = parse_guest_name(booking)
    site = booking.get("listing_site") or "direct"

    # Idempotency: search Plane for existing ticket referencing this OR booking ID
    marker = f"OR-BOOKING-{booking_id}"
    for t in existing_tickets:
        desc = t.get("description_html") or ""
        if marker in desc:
            return f"skip ({ISSUE_PREFIX}-{t['sequence_id']} exists)"

    # Priority based on lead time
    days_out = days_until(arrival)
    if days_out <= 7:
        priority = "urgent"
    elif days_out <= 21:
        priority = "high"
    else:
        priority = "medium"

    target_date = (arrival - timedelta(days=1)).isoformat()
    title = f"Prep property for {guest} ({arrival.isoformat()} -> {departure.isoformat()}, {nights}n via {site})"

    description = (
        f"<p><strong>OR Booking:</strong> {marker}<br>"
        f"<strong>Guest:</strong> {guest}<br>"
        f"<strong>Channel:</strong> {site}<br>"
        f"<strong>Arrival:</strong> {arrival.isoformat()} {booking.get('check_in','16:00')}<br>"
        f"<strong>Departure:</strong> {departure.isoformat()} {booking.get('check_out','15:00')}<br>"
        f"<strong>Nights:</strong> {nights}<br>"
        f"<strong>Status:</strong> {booking.get('status')}</p>"
        "<p><strong>Prep checklist (target: arrival eve):</strong></p>"
        "<ul>"
        "<li>[ ] Confirm cleaner schedule for turnover</li>"
        "<li>[ ] Verify keys / smart lock code set for guest</li>"
        "<li>[ ] Stock consumables (toiletries, paper, basic kitchen)</li>"
        "<li>[ ] Test HVAC and property amenities</li>"
        "<li>[ ] Send welcome message + check-in instructions to guest</li>"
        "<li>[ ] Set thermostat for arrival</li>"
        "<li>[ ] Confirm outdoor/grill areas ready</li>"
        "</ul>"
        f"<p><a href='https://app.ownerrez.com/bookings/{booking_id}'>Open booking in OwnerRez</a></p>"
    )

    issue = plane.create_issue({
        "name": title[:200],
        "priority": priority,
        "target_date": target_date,
        "state": STATE_TODO,
        "description_html": description,
    })

    # Tag with module + labels
    labels = {l["name"]: l["id"] for l in plane._request("GET", "/labels/").get("results", [])}
    modules = {m["name"]: m["id"] for m in plane._request("GET", "/modules/").get("results", [])}
    plane.patch_issue(issue["id"], {"labels": [labels.get("revenue-impact"), labels.get("recurring")]})
    if "OPERATIONS" in modules:
        try:
            plane._request("POST", f"/modules/{modules['OPERATIONS']}/module-issues/", {"issues": [issue["id"]]})
        except Exception:
            pass

    return f"created {ISSUE_PREFIX}-{issue['sequence_id']}"


def main() -> int:
    print("[or-sync] fetching current OR bookings...")
    resp = or_request("/bookings", {
        "property_ids": OR_PROPERTY_ID,
        "since_utc": "2026-01-01",
    })
    bookings = resp.get("items", [])
    print(f"[or-sync] {len(bookings)} bookings found")

    print("[or-sync] fetching existing Plane tickets for dedup...")
    plane = PlaneClient()
    existing = plane.list_issues()

    print(f"[or-sync] processing {len(bookings)} bookings...\n")
    for b in bookings:
        result = upsert_prep_ticket(plane, b, existing)
        print(f"  Booking {b['id']} ({b['arrival']} -> {b['departure']}): {result}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
