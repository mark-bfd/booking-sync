# booking-sync

**Part 3 of a portfolio series: cross-SaaS orchestration — two vendor APIs, idempotent sync, designed to backstop webhook gaps.**

Syncs vacation-rental bookings from [OwnerRez](https://www.ownerrez.com/) (channel manager) into prep tickets in [Plane](https://plane.so/) (self-hosted project management). Every confirmed booking becomes an operations ticket with a turnover checklist, a target date of arrival-minus-one-day, and a priority derived from lead time.

**Impact:** every confirmed booking becomes a prep ticket with checklist and deadline in under a minute, unattended — and a dropped webhook can no longer turn into a missed turnover.

## Why polling *and* webhooks

Webhooks are the low-latency path, but they fail silently: endpoints go down, deliveries get dropped, and events that fired while a receiver was offline are gone. This sync is the backstop — an idempotent reconciliation pass that can run on cron (or as a one-time backfill) and converge the two systems to the same state no matter how many events were missed. Running it twice is always safe.

## What it does

- `or_to_plane_sync.py` — the sync. Pulls current bookings from the OwnerRez v2 API, deduplicates against existing Plane tickets, and creates a prep ticket per new booking:
  - **Idempotency guard:** each ticket embeds an `OR-BOOKING-<id>` marker in its description; the sync scans existing tickets for that marker and skips matches. No state file, no database — the target system *is* the state.
  - **Lead-time priority:** arrival within 7 days = urgent, within 21 = high, else medium.
  - **Ticket enrichment:** guest, channel, arrival/departure, nights, a prep checklist, and a deep link back to the booking in OwnerRez. Tagged with labels and attached to an OPERATIONS module when present.
  - Handles both native OwnerRez bookings and iCal-imported ones (guest name parsed from iCal notes).
- `plane_issue.py` — a zero-dependency Plane API client that also works as a standalone CLI. It works around a real bug in self-hosted Plane: the API doesn't reliably assign `sequence_id = max+1`, which produces duplicate issue numbers. The client hints the correct sequence at POST time, **verifies the value the server actually persisted**, and PATCHes to a fresh unique number if a collision slipped through.

## Architecture

```
OwnerRez v2 API                       Plane REST API (self-hosted)
 (bookings, Basic auth)                (issues, X-API-Key)
        |                                     ^
        v                                     |
  or_to_plane_sync.py  ------------------> plane_issue.PlaneClient
   1. GET /bookings                        collision-safe create,
   2. GET existing issues                  verify-after-create,
   3. skip if OR-BOOKING-<id> marker found PATCH fallback
   4. create prep ticket (arrival - 1 day)
```

The webhook path (OwnerRez -> automation platform -> Plane) can coexist; this job just guarantees eventual consistency underneath it.

## Setup

No third-party Python packages — standard library only (Python 3.10+).

All configuration is via environment variables:

| Env var | Purpose |
|---|---|
| `OWNERREZ_USERNAME` | OwnerRez account email that owns the API token (a token issued under one login returns 401 for others) |
| `OWNERREZ_API_KEY` | OwnerRez personal access token |
| `OWNERREZ_PROPERTY_ID` | Numeric property ID to sync |
| `PLANE_API_KEY` | Plane API key |
| `PLANE_HOST` | e.g. `http://192.0.2.10:8083` (your Plane server) |
| `PLANE_WORKSPACE` | Workspace slug, e.g. `your-workspace` |
| `PLANE_PROJECT_ID` | Target project UUID |
| `PLANE_ISSUE_PREFIX` | Display prefix for issue numbers, e.g. `PROJ` |
| `PLANE_STATE_*` | Per-project state UUIDs (`BACKLOG/TODO/INPROGRESS/DONE/CANCELLED`) — fetch once from `GET .../states/` |

## Usage

```bash
# one-time backfill, then cron it (e.g. every 30 minutes)
python or_to_plane_sync.py
```

```
[or-sync] fetching current OR bookings...
[or-sync] 12 bookings found
[or-sync] fetching existing Plane tickets for dedup...
[or-sync] processing 12 bookings...

  Booking 101 (2026-07-20 -> 2026-07-23): skip (PROJ-41 exists)
  Booking 102 (2026-08-02 -> 2026-08-06): created PROJ-57
```

The Plane client is also a CLI in its own right:

```bash
python plane_issue.py create --name "Replace furnace filter" --priority high --target 2026-08-01
python plane_issue.py next-seq
```

## Stack

- Python 3.10+, standard library only (`urllib`, `json`, `datetime`)
- OwnerRez REST v2 (HTTP Basic auth) — bookings as the source of truth
- Plane REST v1 (self-hosted) — tickets as the operational surface

## License

MIT — see [LICENSE](LICENSE).
