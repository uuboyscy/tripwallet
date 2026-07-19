import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

test_data_directory = tempfile.TemporaryDirectory()
os.environ["TRIPWALLET_DB_PATH"] = str(Path(test_data_directory.name) / "tripwallet-test.db")

from app.main import DATABASE_PATH, Trip, User, app  # noqa: E402
from app.storage import SQLiteStore  # noqa: E402


client = TestClient(app)


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def signup(email: str, display_name: str = "User") -> str:
    response = client.post(
        "/auth/signup",
        json={"email": email, "password": "secret123", "display_name": display_name},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def test_trip_flow_and_permissions() -> None:
    owner_token = signup("owner@example.com", "Owner")
    member_token = signup("member@example.com", "Member")

    trip_resp = client.post(
        "/trips",
        headers=auth_header(owner_token),
        json={"name": "Tokyo", "base_currency": "JPY"},
    )
    assert trip_resp.status_code == 200
    trip_id = trip_resp.json()["id"]

    invite_resp = client.post(
        f"/trips/{trip_id}/invite",
        headers=auth_header(owner_token),
        json={"expires_in_hours": 24},
    )
    assert invite_resp.status_code == 200

    code = invite_resp.json()["invite_code"]
    preview_resp = client.get(f"/invites/{code}")
    assert preview_resp.status_code == 200
    assert preview_resp.json()["trip_id"] == trip_id
    assert preview_resp.json()["trip_name"] == "Tokyo"
    assert preview_resp.json()["base_currency"] == "JPY"

    join_resp = client.post("/trips/join", headers=auth_header(member_token), json={"invite_code": code})
    assert join_resp.status_code == 201
    assert join_resp.json() == {"status": "joined", "trip_id": trip_id}

    expense_resp = client.post(
        f"/trips/{trip_id}/expenses",
        headers=auth_header(member_token),
        json={
            "amount": "10",
            "currency": "USD",
            "fx_rate_to_base": "150",
            "title": "Sushi dinner",
            "category": "food",
            "expense_time": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert expense_resp.status_code == 201
    expense_id = expense_resp.json()["id"]
    assert expense_resp.json()["amount_in_base"] == "1500"
    assert expense_resp.json()["title"] == "Sushi dinner"

    owner_edit = client.patch(
        f"/trips/{trip_id}/expenses/{expense_id}",
        headers=auth_header(owner_token),
        json={"note": "owner tries edit"},
    )
    assert owner_edit.status_code == 403

    analytics_resp = client.get(f"/trips/{trip_id}/analytics/summary", headers=auth_header(owner_token))
    assert analytics_resp.status_code == 200
    body = analytics_resp.json()
    assert body["total_spending_in_base"] == "1500"
    assert body["total_spending_by_category"]["food"] == "1500"

    member_edit = client.patch(
        f"/trips/{trip_id}/expenses/{expense_id}",
        headers=auth_header(member_token),
        json={"note": "updated in SQLite"},
    )
    assert member_edit.status_code == 200
    assert member_edit.json()["note"] == "updated in SQLite"

    delete_response = client.delete(
        f"/trips/{trip_id}/expenses/{expense_id}",
        headers=auth_header(member_token),
    )
    assert delete_response.status_code == 204

    expenses_response = client.get(
        f"/trips/{trip_id}/expenses",
        headers=auth_header(owner_token),
    )
    assert expenses_response.status_code == 200
    assert expenses_response.json() == []

    member_response = client.get("/me", headers=auth_header(member_token))
    assert member_response.status_code == 200

    remove_member_response = client.delete(
        f"/trips/{trip_id}/members/{member_response.json()['id']}",
        headers=auth_header(owner_token),
    )
    assert remove_member_response.status_code == 204

    members_response = client.get(
        f"/trips/{trip_id}/members",
        headers=auth_header(owner_token),
    )
    assert members_response.status_code == 200
    assert len(members_response.json()) == 1


def test_ui_page_available() -> None:
    response = client.get('/ui')
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate"
    assert response.headers["pragma"] == "no-cache"
    assert 'TripWallet' in response.text
    assert 'Quick Access' in response.text
    assert 'class="language-select' in response.text
    assert '<option value="en">English</option>' in response.text
    assert '<option value="zh-TW">繁體中文</option>' in response.text
    assert '<option value="ja">日本語</option>' in response.text
    assert 'id="remember-password"' in response.text
    assert 'Remember password' in response.text
    assert 'tripwallet_remembered_credentials' in response.text
    assert 'id="pending-invite-banner"' in response.text
    assert 'function buildInviteLink' in response.text
    assert 'Copy invite link' in response.text
    assert 'id="trip-loading-banner"' in response.text
    assert 'const API_TIMEOUT_MS = 15000' in response.text
    assert 'const controller = new AbortController()' in response.text
    assert 'state.tripContextRequestId' in response.text
    assert 'state.tripSwitchRollback' in response.text
    assert 'function refreshCurrentTripContext' in response.text
    assert 'onclick="refreshTrips()"' in response.text
    assert 'id="header-trip-select"' in response.text
    assert 'aria-label="Switch trip"' in response.text
    assert 'onchange="selectTrip(this.value)"' in response.text
    assert 'id="trip-list" class="space-y-3"' in response.text


def test_trip_list_returns_every_trip_for_the_user() -> None:
    token = signup("multiple-trips@example.com", "Multiple Trips")

    for name, currency in (("Taipei", "TWD"), ("Tokyo", "JPY"), ("Paris", "EUR")):
        response = client.post(
            "/trips",
            headers=auth_header(token),
            json={"name": name, "base_currency": currency},
        )
        assert response.status_code == 200

    response = client.get("/trips", headers=auth_header(token))
    assert response.status_code == 200
    assert [(trip["name"], trip["base_currency"]) for trip in response.json()] == [
        ("Taipei", "TWD"),
        ("Tokyo", "JPY"),
        ("Paris", "EUR"),
    ]


def test_expense_uses_latest_fx_and_target_currency() -> None:
    token = signup("fx@example.com", "FX User")

    trip_resp = client.post(
        "/trips",
        headers=auth_header(token),
        json={"name": "Taipei", "base_currency": "TWD"},
    )
    assert trip_resp.status_code == 200
    trip_id = trip_resp.json()["id"]

    expense_resp = client.post(
        f"/trips/{trip_id}/expenses",
        headers=auth_header(token),
        json={
            "amount": "100",
            "currency": "USD",
            "target_currency": "JPY",
            "category": "transport",
            "expense_time": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert expense_resp.status_code == 201
    body = expense_resp.json()
    assert body["target_currency"] == "JPY"
    assert body["currency"] == "USD"
    assert body["fx_rate_to_base"] == "32.25806451612903225806451613"
    assert body["fx_rate_to_target"] == "149.2537313432835820895522388"


def test_default_testing_accounts_can_login() -> None:
    user1 = client.post("/auth/login", json={"email": "user1@example.com", "password": "123456"})
    assert user1.status_code == 200
    assert user1.json()["access_token"]

    user2 = client.post("/auth/login", json={"email": "user2@example.com", "password": "123456"})
    assert user2.status_code == 200
    assert user2.json()["access_token"]


def test_data_is_persisted_in_sqlite() -> None:
    token = signup("persistent@example.com", "Persistent User")
    trip_response = client.post(
        "/trips",
        headers=auth_header(token),
        json={"name": "Persistent Trip", "base_currency": "TWD"},
    )
    assert trip_response.status_code == 200

    reopened_store = SQLiteStore(DATABASE_PATH)
    reopened_store.initialize()

    user_data = reopened_store.get_user_by_email("persistent@example.com")
    trip_data = reopened_store.get_trip(trip_response.json()["id"])

    assert DATABASE_PATH.is_file()
    assert user_data is not None
    assert User.model_validate_json(user_data).display_name == "Persistent User"
    assert trip_data is not None
    assert Trip.model_validate_json(trip_data).name == "Persistent Trip"
