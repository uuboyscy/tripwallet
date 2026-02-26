from __future__ import annotations

import hashlib
import secrets
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Annotated
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field

app = FastAPI(title="TripWallet API", version="0.2.0")

JWT_SECRET = "dev-secret-change-me"
JWT_ALGO = "HS256"


class TripStatus(str, Enum):
    active = "active"
    archived = "archived"


class MemberRole(str, Enum):
    owner = "owner"
    member = "member"


class User(BaseModel):
    id: UUID
    email: EmailStr
    password_hash: str
    display_name: str
    created_at: datetime


class Trip(BaseModel):
    id: UUID
    owner_user_id: UUID
    name: str
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str
    status: TripStatus = TripStatus.active
    created_at: datetime


class TripMember(BaseModel):
    id: UUID
    trip_id: UUID
    user_id: UUID
    role: MemberRole
    nickname_in_trip: str | None = None
    joined_at: datetime


class TripInvite(BaseModel):
    id: UUID
    trip_id: UUID
    invite_code: str
    expires_at: datetime | None = None
    is_active: bool = True
    created_at: datetime
    created_by_user_id: UUID


class Expense(BaseModel):
    id: UUID
    trip_id: UUID
    created_by_user_id: UUID
    owner_user_id: UUID
    paid_by_user_id: UUID
    amount: Decimal
    currency: str
    fx_rate_to_base: Decimal
    amount_in_base: Decimal
    category: str
    note: str | None = None
    split_mode: str
    split_with_user_ids: list[UUID]
    custom_split_amounts: dict[str, Decimal] | None = None
    expense_time: datetime
    created_at: datetime
    updated_at: datetime


# request/response schemas
class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    display_name: str = Field(min_length=1)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: UUID
    email: EmailStr
    display_name: str


class CreateTripRequest(BaseModel):
    name: str
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str = Field(min_length=3, max_length=3)


class TripResponse(BaseModel):
    id: UUID
    owner_user_id: UUID
    name: str
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str
    status: TripStatus


class JoinTripRequest(BaseModel):
    invite_code: str


class InviteRequest(BaseModel):
    expires_in_hours: int | None = Field(default=24, ge=1, le=720)


class InviteResponse(BaseModel):
    invite_code: str
    expires_at: datetime | None


class MemberResponse(BaseModel):
    user_id: UUID
    role: MemberRole
    display_name: str
    nickname_in_trip: str | None = None


class CreateExpenseRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    fx_rate_to_base: Decimal | None = Field(default=None, gt=0)
    category: str
    expense_time: datetime
    paid_by_user_id: UUID | None = None
    owner_user_id: UUID | None = None
    note: str | None = None
    split_mode: str = Field(default="equal")
    split_with_user_ids: list[UUID] | None = None
    custom_split_amounts: dict[str, Decimal] | None = None


class UpdateExpenseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    fx_rate_to_base: Decimal | None = Field(default=None, gt=0)
    category: str | None = None
    expense_time: datetime | None = None
    paid_by_user_id: UUID | None = None
    owner_user_id: UUID | None = None
    note: str | None = None
    split_mode: str | None = None
    split_with_user_ids: list[UUID] | None = None
    custom_split_amounts: dict[str, Decimal] | None = None


class ExpenseResponse(BaseModel):
    id: UUID
    trip_id: UUID
    created_by_user_id: UUID
    owner_user_id: UUID
    paid_by_user_id: UUID
    amount: Decimal
    currency: str
    fx_rate_to_base: Decimal
    amount_in_base: Decimal
    category: str
    note: str | None = None
    split_mode: str
    split_with_user_ids: list[UUID]
    custom_split_amounts: dict[str, Decimal] | None = None
    expense_time: datetime


class SummaryResponse(BaseModel):
    total_spending_in_base: Decimal
    total_spending_by_member: dict[str, Decimal]
    total_spending_by_category: dict[str, Decimal]
    total_spending_by_day: dict[str, Decimal]


users: dict[UUID, User] = {}
users_by_email: dict[str, UUID] = {}
trips: dict[UUID, Trip] = {}
trip_members: dict[UUID, list[TripMember]] = defaultdict(list)
trip_invites: dict[UUID, TripInvite] = {}
invite_index: dict[str, UUID] = {}
expenses: dict[UUID, list[Expense]] = defaultdict(list)


def now_utc() -> datetime:
    return datetime.now(UTC)


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def issue_token(user: User) -> str:
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "exp": int((now_utc() + timedelta(days=2)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def parse_token(auth_header: str | None) -> User:
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")

    token = auth_header.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        user_id = UUID(payload["sub"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    user = users.get(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return user


def current_user(authorization: Annotated[str | None, Header()] = None) -> User:
    return parse_token(authorization)


def ensure_membership(trip_id: UUID, user_id: UUID) -> TripMember:
    for member in trip_members.get(trip_id, []):
        if member.user_id == user_id:
            return member
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a trip member")


def ensure_owner(trip_id: UUID, user_id: UUID) -> None:
    member = ensure_membership(trip_id, user_id)
    if member.role != MemberRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner role required")


def serialize_user(user: User) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, display_name=user.display_name)


def serialize_trip(trip: Trip) -> TripResponse:
    return TripResponse(**trip.model_dump())


def normalize_currency(currency: str) -> str:
    return currency.upper()


def member_ids_for_trip(trip_id: UUID) -> set[UUID]:
    return {member.user_id for member in trip_members.get(trip_id, [])}


def normalize_split(
    trip_id: UUID,
    amount: Decimal,
    split_mode: str,
    split_with_user_ids: list[UUID] | None,
    custom_split_amounts: dict[str, Decimal] | None,
) -> tuple[str, list[UUID], dict[str, Decimal] | None]:
    members = member_ids_for_trip(trip_id)

    if split_mode not in {"equal", "custom"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="split_mode must be equal or custom")

    chosen_members = split_with_user_ids or list(members)
    if not chosen_members:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="split_with_user_ids cannot be empty")
    if any(user_id not in members for user_id in chosen_members):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="split_with_user_ids must be trip members")

    if split_mode == "equal":
        return split_mode, chosen_members, None

    if not custom_split_amounts:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="custom_split_amounts is required")

    normalized: dict[str, Decimal] = {}
    for user_id, value in custom_split_amounts.items():
        uid = UUID(user_id)
        if uid not in members:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="custom split user must be a trip member")
        amount_value = Decimal(value)
        if amount_value < 0:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="custom split amount must be >= 0")
        normalized[str(uid)] = amount_value

    if set(normalized.keys()) != {str(user_id) for user_id in chosen_members}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="custom split users must match split_with_user_ids",
        )
    if sum(normalized.values(), start=Decimal("0")) != amount:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="custom split must sum to amount")
    return split_mode, chosen_members, normalized




@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "tripwallet", "ui": "/ui", "docs": "/docs"}


@app.get("/ui", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse("app/static/ui.html")

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/signup", response_model=AuthResponse)
def signup(payload: SignupRequest) -> AuthResponse:
    email = payload.email.lower()
    if email in users_by_email:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    user = User(
        id=uuid4(),
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        created_at=now_utc(),
    )
    users[user.id] = user
    users_by_email[email] = user.id
    return AuthResponse(access_token=issue_token(user))


@app.post("/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    user_id = users_by_email.get(payload.email.lower())
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    user = users[user_id]
    if user.password_hash != hash_password(payload.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    return AuthResponse(access_token=issue_token(user))


@app.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)) -> UserResponse:
    return serialize_user(user)


@app.post("/trips", response_model=TripResponse)
def create_trip(payload: CreateTripRequest, user: User = Depends(current_user)) -> TripResponse:
    trip = Trip(
        id=uuid4(),
        owner_user_id=user.id,
        name=payload.name,
        start_date=payload.start_date,
        end_date=payload.end_date,
        base_currency=normalize_currency(payload.base_currency),
        status=TripStatus.active,
        created_at=now_utc(),
    )
    trips[trip.id] = trip
    trip_members[trip.id].append(
        TripMember(
            id=uuid4(),
            trip_id=trip.id,
            user_id=user.id,
            role=MemberRole.owner,
            joined_at=now_utc(),
        )
    )
    return serialize_trip(trip)


@app.get("/trips", response_model=list[TripResponse])
def list_trips(user: User = Depends(current_user)) -> list[TripResponse]:
    joined_trip_ids = {m.trip_id for ms in trip_members.values() for m in ms if m.user_id == user.id}
    return [serialize_trip(trips[trip_id]) for trip_id in joined_trip_ids]


@app.get("/trips/{trip_id}", response_model=TripResponse)
def get_trip(trip_id: UUID, user: User = Depends(current_user)) -> TripResponse:
    ensure_membership(trip_id, user.id)
    trip = trips.get(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    return serialize_trip(trip)


@app.post("/trips/{trip_id}/invite", response_model=InviteResponse)
def create_invite(trip_id: UUID, payload: InviteRequest, user: User = Depends(current_user)) -> InviteResponse:
    ensure_owner(trip_id, user.id)
    if trip_id not in trips:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    existing = trip_invites.get(trip_id)
    if existing:
        existing.is_active = False
        invite_index.pop(existing.invite_code, None)

    code = secrets.token_urlsafe(6)
    expires_at = None
    if payload.expires_in_hours is not None:
        expires_at = now_utc() + timedelta(hours=payload.expires_in_hours)

    invite = TripInvite(
        id=uuid4(),
        trip_id=trip_id,
        invite_code=code,
        expires_at=expires_at,
        is_active=True,
        created_at=now_utc(),
        created_by_user_id=user.id,
    )
    trip_invites[trip_id] = invite
    invite_index[code] = trip_id
    return InviteResponse(invite_code=code, expires_at=expires_at)


@app.post("/trips/join", status_code=status.HTTP_201_CREATED)
def join_trip(payload: JoinTripRequest, user: User = Depends(current_user)) -> dict[str, str]:
    trip_id = invite_index.get(payload.invite_code)
    if not trip_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite code not found")

    invite = trip_invites[trip_id]
    if not invite.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invite inactive")
    if invite.expires_at and invite.expires_at < now_utc():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invite expired")

    for member in trip_members[trip_id]:
        if member.user_id == user.id:
            return {"status": "already_joined"}

    trip_members[trip_id].append(
        TripMember(
            id=uuid4(),
            trip_id=trip_id,
            user_id=user.id,
            role=MemberRole.member,
            joined_at=now_utc(),
        )
    )
    return {"status": "joined"}


@app.get("/trips/{trip_id}/members", response_model=list[MemberResponse])
def list_members(trip_id: UUID, user: User = Depends(current_user)) -> list[MemberResponse]:
    ensure_membership(trip_id, user.id)
    return [
        MemberResponse(
            user_id=m.user_id,
            role=m.role,
            display_name=users[m.user_id].display_name,
            nickname_in_trip=m.nickname_in_trip,
        )
        for m in trip_members[trip_id]
    ]


@app.delete("/trips/{trip_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def remove_member(trip_id: UUID, user_id: UUID, user: User = Depends(current_user)) -> None:
    ensure_owner(trip_id, user.id)
    trip = trips.get(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    if user_id == trip.owner_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove owner")

    members = trip_members[trip_id]
    new_members = [m for m in members if m.user_id != user_id]
    trip_members[trip_id] = new_members
    return None


@app.post("/trips/{trip_id}/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def create_expense(trip_id: UUID, payload: CreateExpenseRequest, user: User = Depends(current_user)) -> ExpenseResponse:
    ensure_membership(trip_id, user.id)
    trip = trips.get(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    currency = normalize_currency(payload.currency)
    if currency == trip.base_currency:
        fx = Decimal("1")
    else:
        if payload.fx_rate_to_base is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fx_rate_to_base is required")
        fx = payload.fx_rate_to_base

    payer_id = payload.paid_by_user_id or user.id
    ensure_membership(trip_id, payer_id)

    owner_id = payload.owner_user_id or user.id
    ensure_membership(trip_id, owner_id)

    split_mode, split_user_ids, custom_split = normalize_split(
        trip_id,
        payload.amount,
        payload.split_mode,
        payload.split_with_user_ids,
        payload.custom_split_amounts,
    )

    amount_in_base = payload.amount * fx
    expense = Expense(
        id=uuid4(),
        trip_id=trip_id,
        created_by_user_id=user.id,
        owner_user_id=owner_id,
        paid_by_user_id=payer_id,
        amount=payload.amount,
        currency=currency,
        fx_rate_to_base=fx,
        amount_in_base=amount_in_base,
        category=payload.category,
        note=payload.note,
        split_mode=split_mode,
        split_with_user_ids=split_user_ids,
        custom_split_amounts=custom_split,
        expense_time=payload.expense_time,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    expenses[trip_id].append(expense)
    return ExpenseResponse(**expense.model_dump())


@app.get("/trips/{trip_id}/expenses", response_model=list[ExpenseResponse])
def list_expenses(
    trip_id: UUID,
    user: User = Depends(current_user),
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    paid_by: UUID | None = None,
    category: str | None = None,
) -> list[ExpenseResponse]:
    ensure_membership(trip_id, user.id)
    items = expenses.get(trip_id, [])

    def include(item: Expense) -> bool:
        if from_date and item.expense_time.date() < from_date:
            return False
        if to_date and item.expense_time.date() > to_date:
            return False
        if paid_by and item.paid_by_user_id != paid_by:
            return False
        if category and item.category != category:
            return False
        return True

    return [ExpenseResponse(**item.model_dump()) for item in items if include(item)]


@app.patch("/trips/{trip_id}/expenses/{expense_id}", response_model=ExpenseResponse)
def update_expense(
    trip_id: UUID,
    expense_id: UUID,
    payload: UpdateExpenseRequest,
    user: User = Depends(current_user),
) -> ExpenseResponse:
    ensure_membership(trip_id, user.id)
    trip = trips.get(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    trip_expenses = expenses.get(trip_id, [])
    for idx, item in enumerate(trip_expenses):
        if item.id != expense_id:
            continue
        updates = payload.model_dump(exclude_unset=True)
        split_fields = {"split_mode", "split_with_user_ids", "custom_split_amounts", "owner_user_id"}
        is_split_only_update = set(updates).issubset(split_fields)
        if item.created_by_user_id != user.id and not is_split_only_update:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot edit others' expenses")

        new_data = item.model_dump()
        new_data.update(updates)

        if "currency" in new_data and new_data["currency"]:
            new_data["currency"] = normalize_currency(new_data["currency"])

        if "paid_by_user_id" in updates and updates["paid_by_user_id"] is not None:
            ensure_membership(trip_id, updates["paid_by_user_id"])
        if "owner_user_id" in updates and updates["owner_user_id"] is not None:
            ensure_membership(trip_id, updates["owner_user_id"])

        currency = new_data["currency"]
        if currency == trip.base_currency:
            new_data["fx_rate_to_base"] = Decimal("1")
        elif new_data.get("fx_rate_to_base") is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fx_rate_to_base is required")

        split_mode, split_user_ids, custom_split = normalize_split(
            trip_id,
            Decimal(new_data["amount"]),
            new_data.get("split_mode", "equal"),
            new_data.get("split_with_user_ids"),
            new_data.get("custom_split_amounts"),
        )
        new_data["split_mode"] = split_mode
        new_data["split_with_user_ids"] = split_user_ids
        new_data["custom_split_amounts"] = custom_split

        new_data["amount_in_base"] = Decimal(new_data["amount"]) * Decimal(new_data["fx_rate_to_base"])
        new_data["updated_at"] = now_utc()

        updated = Expense(**new_data)
        trip_expenses[idx] = updated
        return ExpenseResponse(**updated.model_dump())

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")


@app.delete(
    "/trips/{trip_id}/expenses/{expense_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_expense(trip_id: UUID, expense_id: UUID, user: User = Depends(current_user)) -> None:
    ensure_membership(trip_id, user.id)
    trip_expenses = expenses.get(trip_id, [])
    for idx, item in enumerate(trip_expenses):
        if item.id == expense_id:
            if item.created_by_user_id != user.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete others' expenses")
            del trip_expenses[idx]
            return None
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")


def compute_summary(items: list[Expense]) -> SummaryResponse:
    total = Decimal("0")
    by_member: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_category: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    by_day: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))

    for item in items:
        total += item.amount_in_base
        by_member[str(item.paid_by_user_id)] += item.amount_in_base
        by_category[item.category] += item.amount_in_base
        by_day[item.expense_time.date().isoformat()] += item.amount_in_base

    return SummaryResponse(
        total_spending_in_base=total,
        total_spending_by_member=dict(by_member),
        total_spending_by_category=dict(by_category),
        total_spending_by_day=dict(by_day),
    )


@app.get("/trips/{trip_id}/analytics/summary", response_model=SummaryResponse)
def analytics_summary(trip_id: UUID, user: User = Depends(current_user)) -> SummaryResponse:
    ensure_membership(trip_id, user.id)
    return compute_summary(expenses.get(trip_id, []))


@app.get("/trips/{trip_id}/analytics/me", response_model=SummaryResponse)
def analytics_me(trip_id: UUID, user: User = Depends(current_user)) -> SummaryResponse:
    ensure_membership(trip_id, user.id)
    mine = [e for e in expenses.get(trip_id, []) if e.created_by_user_id == user.id]
    return compute_summary(mine)
