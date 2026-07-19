# TripWallet

TripWallet is a lightweight trip-based expense sharing app MVP with:
- FastAPI backend API
- Built-in web UI at `/ui` for manual end-to-end use
- Multi-currency expense support with base-currency analytics

## Implemented MVP
- Auth: `POST /auth/signup`, `POST /auth/login`, `GET /me`
- Trips: create/list/get, invite generation, join by invite code
- Members: list and owner removal
- Expenses: create/list/update/delete with member authorization rules
- Analytics:
  - `GET /trips/{trip_id}/analytics/summary`
  - `GET /trips/{trip_id}/analytics/me`
- UI:
  - `GET /ui` (single-page MVP UI for auth, trips, invites, expenses, analytics)

## Run locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

The project pins Python 3.13 in `.python-version`; uv downloads it automatically when needed.

Run the test suite with:

```bash
uv run pytest
```

## Data storage

TripWallet stores users, trips, members, invites, and expenses in SQLite. By default, the database file is:

```text
data/tripwallet.db
```

The directory and database are created automatically. To store the database elsewhere, set an absolute or project-relative path:

```bash
TRIPWALLET_DB_PATH=/path/to/tripwallet.db uv run uvicorn app.main:app --host 127.0.0.1 --port 8080
```

For a simple backup, stop the app and copy `data/tripwallet.db` to a safe location. The manual GitHub Action uses an ephemeral runner, so its database is deleted when the workflow ends.

Open:
- API docs: `http://127.0.0.1:8080/docs`
- MVP UI: `http://127.0.0.1:8080/ui`

### Cloudflare Tunnel (this Mac)

The local TripWallet service listens on `127.0.0.1:8080`. Configure the
Cloudflare Tunnel published application service URL as:

```text
http://127.0.0.1:8080
```

## Security and behavior notes
- Use `Authorization: Bearer <token>` for authenticated API calls.
- Only trip members can access trip resources.
- Members can edit/delete only expenses they created.
- Multi-currency rule:
  - if `currency == trip.base_currency`, `fx_rate_to_base = 1`
  - else `fx_rate_to_base` is required

## GitHub Action: manual public URL via ngrok
Workflow: `.github/workflows/mobile-preview.yml`

Manual trigger behavior:
1. install dependencies
2. start app on `:8000`
3. configure ngrok using repository secret `NGROK_TOKEN`
4. print public tunnel URL(s)
5. keep job alive until manually cancelled

## Current architecture note

This small self-hosted deployment uses SQLite with WAL mode. If the service later needs multiple application servers or substantially more concurrent writes, migrate the storage layer to Postgres.
