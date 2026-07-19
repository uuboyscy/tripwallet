from __future__ import annotations

import hashlib
import os
import re
import secrets
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from .storage import SQLiteStore

app = FastAPI(title="TripWallet API", version="0.2.0")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
configured_database_path = Path(os.getenv("TRIPWALLET_DB_PATH", "data/tripwallet.db")).expanduser()
DATABASE_PATH = (
    configured_database_path
    if configured_database_path.is_absolute()
    else PROJECT_ROOT / configured_database_path
)

JWT_SECRET = "dev-secret-change-me"
JWT_ALGO = "HS256"

CURRENCY_ALIASES = {
    "EU": "EUR",
    "POUND": "GBP",
    "RMB": "CNY",
}

# Approximate market rates in USD for 1 unit of each currency.
USD_VALUE_BY_CURRENCY: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "GBP": Decimal("1.27"),
    "JPY": Decimal("0.0067"),
    "TWD": Decimal("0.031"),
    "CNY": Decimal("0.139"),
}

DEFAULT_TEST_ACCOUNTS = (
    {"email": "user1@example.com", "display_name": "user1", "password": "123456"},
    {"email": "user2@example.com", "display_name": "user2", "password": "123456"},
)


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
    invited_name: str
    invited_name_key: str
    is_active: bool = True
    claimed_by_user_id: UUID | None = None
    claimed_at: datetime | None = None
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
    target_currency: str
    fx_rate_to_target: Decimal
    amount_in_target: Decimal
    fx_rate_to_base: Decimal
    amount_in_base: Decimal
    title: str | None = None
    category: str
    note: str | None = None
    split_mode: str
    split_with_user_ids: list[UUID]
    custom_split_amounts: dict[str, Decimal] | None = None
    expense_time: datetime
    created_at: datetime
    updated_at: datetime


# request/response schemas
def clean_label(value: str, field_name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", value).strip()
    if not cleaned:
        raise ValueError(f"{field_name} cannot be blank")
    if len(cleaned) > 100:
        raise ValueError(f"{field_name} must be 100 characters or fewer")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError(f"{field_name} contains unsupported control characters")
    if "<" in cleaned or ">" in cleaned:
        raise ValueError(f"{field_name} cannot contain angle brackets")
    return cleaned


def clean_currency(value: str) -> str:
    normalized = normalize_currency(value)
    if not re.fullmatch(r"[A-Z]{2,8}", normalized):
        raise ValueError("currency must contain 2 to 8 letters")
    return normalized


def validate_trip_dates(start_date: date | None, end_date: date | None) -> None:
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be provided together")
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("start_date cannot be later than end_date")


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
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str = Field(min_length=2, max_length=8)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return clean_label(value, "name")

    @field_validator("base_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        return clean_currency(value)

    @model_validator(mode="after")
    def validate_dates(self) -> "CreateTripRequest":
        validate_trip_dates(self.start_date, self.end_date)
        return self


class UpdateTripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str | None = Field(default=None, min_length=2, max_length=8)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        return clean_label(value, "name") if value is not None else None

    @field_validator("base_currency")
    @classmethod
    def validate_currency(cls, value: str | None) -> str | None:
        return clean_currency(value) if value is not None else None

    @model_validator(mode="after")
    def require_an_update(self) -> "UpdateTripRequest":
        if not self.model_fields_set:
            raise ValueError("at least one trip field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        if "base_currency" in self.model_fields_set and self.base_currency is None:
            raise ValueError("base_currency cannot be null")
        return self


class TripResponse(BaseModel):
    id: UUID
    owner_user_id: UUID
    name: str
    start_date: date | None = None
    end_date: date | None = None
    base_currency: str
    status: TripStatus


class JoinTripRequest(BaseModel):
    invite_code: str = Field(min_length=4, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class InviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invited_name: str = Field(min_length=1, max_length=100)

    @field_validator("invited_name")
    @classmethod
    def validate_invited_name(cls, value: str) -> str:
        return clean_label(value, "invited_name")


class InviteResponse(BaseModel):
    invite_code: str
    invited_name: str
    is_claimed: bool = False


class InviteListItemResponse(InviteResponse):
    claimed_by_user_id: UUID | None = None


class InvitePreviewResponse(BaseModel):
    invite_code: str
    trip_id: UUID
    trip_name: str
    base_currency: str
    invited_name: str


class MemberResponse(BaseModel):
    user_id: UUID
    role: MemberRole
    display_name: str
    nickname_in_trip: str | None = None


class CreateExpenseRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=2, max_length=8)
    target_currency: str | None = Field(default=None, min_length=2, max_length=8)
    fx_rate_to_target: Decimal | None = Field(default=None, gt=0)
    fx_rate_to_base: Decimal | None = Field(default=None, gt=0)
    title: str | None = None
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
    currency: str | None = Field(default=None, min_length=2, max_length=8)
    target_currency: str | None = Field(default=None, min_length=2, max_length=8)
    fx_rate_to_target: Decimal | None = Field(default=None, gt=0)
    fx_rate_to_base: Decimal | None = Field(default=None, gt=0)
    title: str | None = None
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
    target_currency: str
    fx_rate_to_target: Decimal
    amount_in_target: Decimal
    fx_rate_to_base: Decimal
    amount_in_base: Decimal
    title: str | None = None
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


storage = SQLiteStore(DATABASE_PATH)
storage.initialize()


def load_user_by_id(user_id: UUID) -> User | None:
    data = storage.get_user_by_id(str(user_id))
    return User.model_validate_json(data) if data else None


def load_user_by_email(email: str) -> User | None:
    data = storage.get_user_by_email(email)
    return User.model_validate_json(data) if data else None


def load_trip(trip_id: UUID) -> Trip | None:
    data = storage.get_trip(str(trip_id))
    return Trip.model_validate_json(data) if data else None


def load_members(trip_id: UUID) -> list[TripMember]:
    return [TripMember.model_validate_json(data) for data in storage.list_members(str(trip_id))]


def load_expenses(trip_id: UUID) -> list[Expense]:
    return [Expense.model_validate_json(data) for data in storage.list_expenses(str(trip_id))]


def ensure_default_test_accounts() -> None:
    for item in DEFAULT_TEST_ACCOUNTS:
        email = item["email"].lower()
        if load_user_by_email(email):
            continue
        try:
            create_user(email=email, password=item["password"], display_name=item["display_name"])
        except sqlite3.IntegrityError:
            pass


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

    user = load_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown user")
    return user


def current_user(authorization: Annotated[str | None, Header()] = None) -> User:
    return parse_token(authorization)


def ensure_membership(trip_id: UUID, user_id: UUID) -> TripMember:
    data = storage.get_member(str(trip_id), str(user_id))
    if data:
        return TripMember.model_validate_json(data)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a trip member")


def ensure_owner(trip_id: UUID, user_id: UUID) -> None:
    member = ensure_membership(trip_id, user_id)
    if member.role != MemberRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner role required")


def serialize_user(user: User) -> UserResponse:
    return UserResponse(id=user.id, email=user.email, display_name=user.display_name)


def serialize_trip(trip: Trip) -> TripResponse:
    return TripResponse(**trip.model_dump())


def load_active_invite(invite_code: str) -> TripInvite:
    invite_data = storage.get_invite_by_code(invite_code)
    if not invite_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite code not found")

    invite = TripInvite.model_validate_json(invite_data)
    if not invite.is_active or invite.claimed_by_user_id is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invite already used")
    return invite


def create_user(email: str, password: str, display_name: str) -> User:
    user = User(
        id=uuid4(),
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        created_at=datetime.now(UTC),
    )
    storage.insert_user(user)
    return user


ensure_default_test_accounts()


def normalize_currency(currency: str) -> str:
    upper = currency.upper().strip()
    return CURRENCY_ALIASES.get(upper, upper)


def latest_fx_rate(source_currency: str, target_currency: str) -> Decimal | None:
    source_to_usd = USD_VALUE_BY_CURRENCY.get(source_currency)
    target_to_usd = USD_VALUE_BY_CURRENCY.get(target_currency)
    if source_to_usd is None or target_to_usd is None:
        return None
    return source_to_usd / target_to_usd


def member_ids_for_trip(trip_id: UUID) -> set[UUID]:
    return {member.user_id for member in load_members(trip_id)}


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
    return FileResponse(
        Path(__file__).resolve().parent / "static/ui.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/signup", response_model=AuthResponse)
def signup(payload: SignupRequest) -> AuthResponse:
    email = payload.email.lower()
    if load_user_by_email(email):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")

    try:
        user = create_user(email=email, password=payload.password, display_name=payload.display_name)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered") from exc
    return AuthResponse(access_token=issue_token(user))


@app.post("/auth/login", response_model=AuthResponse)
def login(payload: LoginRequest) -> AuthResponse:
    user = load_user_by_email(payload.email.lower())
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
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
    owner = TripMember(
        id=uuid4(),
        trip_id=trip.id,
        user_id=user.id,
        role=MemberRole.owner,
        joined_at=now_utc(),
    )
    storage.insert_trip_with_owner(trip, owner)
    return serialize_trip(trip)


@app.get("/trips", response_model=list[TripResponse])
def list_trips(user: User = Depends(current_user)) -> list[TripResponse]:
    return [
        serialize_trip(Trip.model_validate_json(data))
        for data in storage.list_trips_for_user(str(user.id))
    ]


@app.get("/trips/{trip_id}", response_model=TripResponse)
def get_trip(trip_id: UUID, user: User = Depends(current_user)) -> TripResponse:
    ensure_membership(trip_id, user.id)
    trip = load_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    return serialize_trip(trip)


@app.patch("/trips/{trip_id}", response_model=TripResponse)
def update_trip(
    trip_id: UUID,
    payload: UpdateTripRequest,
    user: User = Depends(current_user),
) -> TripResponse:
    ensure_owner(trip_id, user.id)
    trip = load_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    updates = payload.model_dump(exclude_unset=True)
    start_date = updates.get("start_date", trip.start_date)
    end_date = updates.get("end_date", trip.end_date)
    try:
        validate_trip_dates(start_date, end_date)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    updated_trip = trip.model_copy(update=updates)
    expenses = load_expenses(trip_id)
    updated_expenses = expenses
    if updated_trip.base_currency != trip.base_currency:
        updated_expenses = []
        old_to_new = latest_fx_rate(trip.base_currency, updated_trip.base_currency)
        for expense in expenses:
            if expense.currency == updated_trip.base_currency:
                rate = Decimal("1")
            else:
                rate = latest_fx_rate(expense.currency, updated_trip.base_currency)
                if rate is None and old_to_new is not None:
                    rate = expense.fx_rate_to_base * old_to_new
            if rate is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Cannot convert {expense.currency} expenses to {updated_trip.base_currency}",
                )
            updated_expenses.append(
                expense.model_copy(
                    update={
                        "fx_rate_to_base": rate,
                        "amount_in_base": expense.amount * rate,
                        "updated_at": now_utc(),
                    }
                )
            )

    try:
        storage.update_trip_with_expenses(updated_trip, updated_expenses)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid trip data") from exc
    return serialize_trip(updated_trip)


@app.post("/trips/{trip_id}/invite", response_model=InviteResponse)
def create_invite(trip_id: UUID, payload: InviteRequest, user: User = Depends(current_user)) -> InviteResponse:
    ensure_owner(trip_id, user.id)
    if not load_trip(trip_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    invited_name_key = unicodedata.normalize("NFKC", payload.invited_name).casefold()
    existing_data = storage.get_invite_for_name(str(trip_id), invited_name_key)
    if existing_data:
        existing = TripInvite.model_validate_json(existing_data)
        if existing.claimed_by_user_id is not None or not existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This person already used their invite link",
            )
        return InviteResponse(
            invite_code=existing.invite_code,
            invited_name=existing.invited_name,
            is_claimed=False,
        )

    invite = TripInvite(
        id=uuid4(),
        trip_id=trip_id,
        invite_code=secrets.token_urlsafe(12),
        invited_name=payload.invited_name,
        invited_name_key=invited_name_key,
        is_active=True,
        created_at=now_utc(),
        created_by_user_id=user.id,
    )
    try:
        storage.insert_invite(invite)
    except sqlite3.IntegrityError:
        concurrent_data = storage.get_invite_for_name(str(trip_id), invited_name_key)
        if not concurrent_data:
            raise
        concurrent = TripInvite.model_validate_json(concurrent_data)
        if concurrent.claimed_by_user_id is not None or not concurrent.is_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This person already used their invite link",
            )
        return InviteResponse(
            invite_code=concurrent.invite_code,
            invited_name=concurrent.invited_name,
            is_claimed=False,
        )
    return InviteResponse(invite_code=invite.invite_code, invited_name=invite.invited_name)


@app.get("/trips/{trip_id}/invites", response_model=list[InviteListItemResponse])
def list_trip_invites(trip_id: UUID, user: User = Depends(current_user)) -> list[InviteListItemResponse]:
    ensure_owner(trip_id, user.id)
    return [
        InviteListItemResponse(
            invite_code=invite.invite_code,
            invited_name=invite.invited_name,
            is_claimed=invite.claimed_by_user_id is not None,
            claimed_by_user_id=invite.claimed_by_user_id,
        )
        for invite in (
            TripInvite.model_validate_json(data)
            for data in storage.list_invites(str(trip_id))
        )
    ]


@app.get("/invites/{invite_code}", response_model=InvitePreviewResponse)
def preview_invite(invite_code: str) -> InvitePreviewResponse:
    invite = load_active_invite(invite_code)
    trip = load_trip(invite.trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    return InvitePreviewResponse(
        invite_code=invite.invite_code,
        trip_id=trip.id,
        trip_name=trip.name,
        base_currency=trip.base_currency,
        invited_name=invite.invited_name,
    )


@app.post("/trips/join", status_code=status.HTTP_201_CREATED)
def join_trip(payload: JoinTripRequest, user: User = Depends(current_user)) -> dict[str, str]:
    invite = load_active_invite(payload.invite_code)
    trip_id = invite.trip_id

    existing_member = storage.get_member(str(trip_id), str(user.id))
    if existing_member:
        return {"status": "already_joined", "trip_id": str(trip_id)}

    try:
        claimed_at = now_utc()
        claimed_invite = invite.model_copy(
            update={
                "is_active": False,
                "claimed_by_user_id": user.id,
                "claimed_at": claimed_at,
            }
        )
        member = TripMember(
            id=uuid4(),
            trip_id=trip_id,
            user_id=user.id,
            role=MemberRole.member,
            nickname_in_trip=invite.invited_name,
            joined_at=claimed_at,
        )
        if not storage.claim_invite(claimed_invite, member):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Invite already used")
    except sqlite3.IntegrityError:
        if storage.get_member(str(trip_id), str(user.id)):
            return {"status": "already_joined", "trip_id": str(trip_id)}
        raise
    return {"status": "joined", "trip_id": str(trip_id)}


@app.get("/trips/{trip_id}/members", response_model=list[MemberResponse])
def list_members(trip_id: UUID, user: User = Depends(current_user)) -> list[MemberResponse]:
    ensure_membership(trip_id, user.id)
    members = load_members(trip_id)
    return [
        MemberResponse(
            user_id=m.user_id,
            role=m.role,
            display_name=load_user_by_id(m.user_id).display_name,
            nickname_in_trip=m.nickname_in_trip,
        )
        for m in members
    ]


@app.delete("/trips/{trip_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def remove_member(trip_id: UUID, user_id: UUID, user: User = Depends(current_user)) -> None:
    ensure_owner(trip_id, user.id)
    trip = load_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")
    if user_id == trip.owner_user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove owner")

    storage.delete_member(str(trip_id), str(user_id))
    return None


@app.post("/trips/{trip_id}/expenses", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
def create_expense(trip_id: UUID, payload: CreateExpenseRequest, user: User = Depends(current_user)) -> ExpenseResponse:
    ensure_membership(trip_id, user.id)
    trip = load_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    currency = normalize_currency(payload.currency)
    target_currency = normalize_currency(payload.target_currency or trip.base_currency)

    if currency == target_currency:
        fx_to_target = Decimal("1")
    elif payload.fx_rate_to_target is not None:
        fx_to_target = payload.fx_rate_to_target
    else:
        auto_fx_target = latest_fx_rate(currency, target_currency)
        if auto_fx_target is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fx_rate_to_target is required")
        fx_to_target = auto_fx_target

    if currency == trip.base_currency:
        fx = Decimal("1")
    elif payload.fx_rate_to_base is not None:
        fx = payload.fx_rate_to_base
    else:
        auto_fx_base = latest_fx_rate(currency, trip.base_currency)
        if auto_fx_base is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fx_rate_to_base is required")
        fx = auto_fx_base

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
    amount_in_target = payload.amount * fx_to_target
    expense = Expense(
        id=uuid4(),
        trip_id=trip_id,
        created_by_user_id=user.id,
        owner_user_id=owner_id,
        paid_by_user_id=payer_id,
        amount=payload.amount,
        currency=currency,
        target_currency=target_currency,
        fx_rate_to_target=fx_to_target,
        amount_in_target=amount_in_target,
        fx_rate_to_base=fx,
        amount_in_base=amount_in_base,
        title=payload.title,
        category=payload.category,
        note=payload.note,
        split_mode=split_mode,
        split_with_user_ids=split_user_ids,
        custom_split_amounts=custom_split,
        expense_time=payload.expense_time,
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    storage.insert_expense(expense)
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
    items = load_expenses(trip_id)

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
    trip = load_trip(trip_id)
    if not trip:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trip not found")

    expense_data = storage.get_expense(str(trip_id), str(expense_id))
    if not expense_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")

    item = Expense.model_validate_json(expense_data)
    if item.created_by_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot edit others' expenses")
    updates = payload.model_dump(exclude_unset=True)
    new_data = item.model_dump()
    new_data.update(updates)

    if "currency" in new_data and new_data["currency"]:
        new_data["currency"] = normalize_currency(new_data["currency"])
    if "target_currency" in new_data and new_data["target_currency"]:
        new_data["target_currency"] = normalize_currency(new_data["target_currency"])

    if "paid_by_user_id" in updates and updates["paid_by_user_id"] is not None:
        ensure_membership(trip_id, updates["paid_by_user_id"])
    if "owner_user_id" in updates and updates["owner_user_id"] is not None:
        ensure_membership(trip_id, updates["owner_user_id"])

    currency = new_data["currency"]
    target_currency = new_data.get("target_currency") or trip.base_currency
    new_data["target_currency"] = target_currency

    if currency == target_currency:
        new_data["fx_rate_to_target"] = Decimal("1")
    elif new_data.get("fx_rate_to_target") is None:
        auto_fx_target = latest_fx_rate(currency, target_currency)
        if auto_fx_target is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fx_rate_to_target is required")
        new_data["fx_rate_to_target"] = auto_fx_target

    if currency == trip.base_currency:
        new_data["fx_rate_to_base"] = Decimal("1")
    elif new_data.get("fx_rate_to_base") is None:
        auto_fx_base = latest_fx_rate(currency, trip.base_currency)
        if auto_fx_base is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="fx_rate_to_base is required")
        new_data["fx_rate_to_base"] = auto_fx_base

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
    new_data["amount_in_target"] = Decimal(new_data["amount"]) * Decimal(new_data["fx_rate_to_target"])
    new_data["updated_at"] = now_utc()

    updated = Expense(**new_data)
    storage.update_expense(updated)
    return ExpenseResponse(**updated.model_dump())


@app.delete(
    "/trips/{trip_id}/expenses/{expense_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_expense(trip_id: UUID, expense_id: UUID, user: User = Depends(current_user)) -> None:
    ensure_membership(trip_id, user.id)
    expense_data = storage.get_expense(str(trip_id), str(expense_id))
    if not expense_data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Expense not found")
    item = Expense.model_validate_json(expense_data)
    if item.created_by_user_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot delete others' expenses")
    storage.delete_expense(str(trip_id), str(expense_id))
    return None


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
    return compute_summary(load_expenses(trip_id))


@app.get("/trips/{trip_id}/analytics/me", response_model=SummaryResponse)
def analytics_me(trip_id: UUID, user: User = Depends(current_user)) -> SummaryResponse:
    ensure_membership(trip_id, user.id)
    mine = [e for e in load_expenses(trip_id) if e.created_by_user_id == user.id]
    return compute_summary(mine)
