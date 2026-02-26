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
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open:
- API docs: `http://127.0.0.1:8000/docs`
- MVP UI: `http://127.0.0.1:8000/ui`

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
This MVP uses in-memory storage for fast iteration. Next step is replacing in-memory stores with Postgres + ORM models/migrations.
