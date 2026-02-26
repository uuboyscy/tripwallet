# Trip Expense Sharing Web App (TripWallet)

## Product Overview
TripWallet is a lightweight, web-based, multi-currency trip expense tracker.

Core flow:
- A user creates a trip.
- Other users join using an invite code.
- Members record expenses.
- The app normalizes expenses into a trip base currency.
- The app shows individual and total spending, plus simple charts.

This is intentionally **not** a full financial settlement system.

## Core Domain Model
The app is centered on a **Trip**.

Each trip has:
- Owner
- Members
- Base currency
- Expenses
- Invite code

Access is trip-isolated: users cannot access trips they are not members of.

## Roles
### Owner
- Creates trip
- Generates/resets invite code
- Archives trip
- Removes members
- Views all expenses

### Member
- Joins via invite code
- Creates expenses
- Edits/deletes only their own expenses
- Views trip statistics

## Functional Requirements
### Authentication
- Email/password login
- JWT-based auth
- Auth required for all API endpoints
- Authorization checks trip membership

### Trip Management
Users can:
- Create trip
- View joined trips
- Join trip with invite code
- View trip details
- View member list

Trip fields:
- `name`
- `start_date`
- `end_date`
- `base_currency`
- `status` (`active` / `archived`)

### Invite Codes
- Generated per trip
- Stored in DB
- Can expire
- Can be deactivated
- Membership (not invite code) controls ongoing access

### Expenses
Expense fields:
- `amount`
- `currency`
- `fx_rate_to_base`
- `amount_in_base`
- `category`
- `note`
- `expense_time`
- `created_by_user_id`
- `paid_by_user_id`
- `trip_id`

Multi-currency strategy:
- If `currency == base_currency`:
  - `fx_rate_to_base = 1`
  - `amount_in_base = amount`
- Otherwise:
  - `fx_rate_to_base` required
  - `amount_in_base = amount * fx_rate_to_base`

`amount_in_base` is stored for simpler analytics.

## Analytics (MVP)
All analytics use `amount_in_base`.

Per-trip summary includes:
- `total_spending_in_base`
- `total_spending_by_member`
- `total_spending_by_category`
- `total_spending_by_day`

## Backend Architecture
- FastAPI
- PostgreSQL
- SQLAlchemy (or SQLModel)
- JWT authentication

### API Endpoints
Auth:
- `POST /auth/signup`
- `POST /auth/login`
- `GET /me`

Trips:
- `POST /trips`
- `GET /trips`
- `GET /trips/{trip_id}`
- `POST /trips/join`
- `POST /trips/{trip_id}/invite`

Expenses:
- `POST /trips/{trip_id}/expenses`
- `GET /trips/{trip_id}/expenses`
- `PATCH /trips/{trip_id}/expenses/{expense_id}`
- `DELETE /trips/{trip_id}/expenses/{expense_id}`

Analytics:
- `GET /trips/{trip_id}/analytics/summary`

## Frontend Architecture
- Next.js (TypeScript)
- React
- Chart.js
- Fetch API or Axios

Pages:
- `/login`
- `/signup`
- `/trips`
- `/trips/[tripId]`
- `/trips/[tripId]/dashboard`

Dashboard charts:
- Bar chart: spending by member
- Pie chart: spending by category
- Line chart: spending by day

## Database Tables
### `users`
- `id` (uuid)
- `email`
- `password_hash`
- `display_name`
- `created_at`

### `trips`
- `id`
- `owner_user_id`
- `name`
- `base_currency`
- `start_date`
- `end_date`
- `status`
- `created_at`

### `trip_members`
- `id`
- `trip_id`
- `user_id`
- `role`
- `joined_at`

### `trip_invites`
- `id`
- `trip_id`
- `invite_code`
- `expires_at`
- `is_active`

### `expenses`
- `id`
- `trip_id`
- `created_by_user_id`
- `paid_by_user_id`
- `amount`
- `currency`
- `fx_rate_to_base`
- `amount_in_base`
- `category`
- `note`
- `expense_time`
- `created_at`
- `updated_at`

## Security Rules
- Every endpoint verifies trip membership.
- Members cannot edit expenses created by other users.
- Invite code alone does not grant read access without membership.

## Development Environment
Ports:
- Backend: `8000`
- Frontend: `3000`
- Postgres: `5432`

Environment variables:
- Backend: `DATABASE_URL`, `JWT_SECRET`
- Frontend: `NEXT_PUBLIC_API_BASE_URL`

## CI & Mobile Testing
GitHub secret:
- `NGROK_TOKEN`

Usage in CI:
- Create public ngrok tunnel
- Enable mobile testing against frontend (`:3000`)

Never print or log secret values.

## Testing Strategy
Backend tests (pytest):
- Auth
- Join trip
- Expense CRUD
- Analytics

CI workflow:
- Manual trigger
- Starts backend + frontend
- Exposes frontend via ngrok

## Non-Goals (MVP)
- Banking system
- Payment processing
- Real-time FX trading logic
- Debt settlement calculations

## Design Principles
- Keep logic simple
- Avoid premature optimization
- Store computed base-currency values
- Authorization first
- Strict trip isolation
