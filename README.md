# TripWallet

TripWallet is a lightweight, trip-based, multi-currency expense sharing backend.

## Implemented (current)
- JWT authentication (`/auth/signup`, `/auth/login`, `/me`)
- Trip management (`/trips`, `/trips/{trip_id}`)
- Invite code workflow (`/trips/{trip_id}/invite`, `/trips/join`)
- Member listing/removal (`/trips/{trip_id}/members`)
- Expense CRUD with multi-currency conversion and `amount_in_base`
- Analytics summary endpoints:
  - `/trips/{trip_id}/analytics/summary`
  - `/trips/{trip_id}/analytics/me`

## Run locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Swagger docs: `http://127.0.0.1:8000/docs`

## API Notes
- Auth: send `Authorization: Bearer <token>`.
- Security:
  - only trip members can access trip resources
  - members can edit/delete only expenses they created
- Multi-currency rule:
  - if expense currency equals trip base currency, fx rate is forced to `1`
  - otherwise `fx_rate_to_base` is required

## GitHub Action: manual public URL (ngrok)
Workflow file: `.github/workflows/mobile-preview.yml`

What it does when manually triggered:
1. installs dependencies
2. starts the FastAPI app on `:8000`
3. configures ngrok from repository secret `NGROK_TOKEN`
4. creates a public tunnel URL and prints it
5. keeps the job alive until you manually cancel the workflow run

## Environment variables (production-ready next step)
- `DATABASE_URL`
- `JWT_SECRET`
- `NEXT_PUBLIC_API_BASE_URL` (for frontend)

> Current implementation uses in-memory storage for rapid MVP iteration.
