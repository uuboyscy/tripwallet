from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app


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
    join_resp = client.post("/trips/join", headers=auth_header(member_token), json={"invite_code": code})
    assert join_resp.status_code == 201

    expense_resp = client.post(
        f"/trips/{trip_id}/expenses",
        headers=auth_header(member_token),
        json={
            "amount": "10",
            "currency": "USD",
            "fx_rate_to_base": "150",
            "category": "food",
            "expense_time": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert expense_resp.status_code == 201
    expense_id = expense_resp.json()["id"]
    assert expense_resp.json()["amount_in_base"] == "1500"

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


def test_ui_page_available() -> None:
    response = client.get('/ui')
    assert response.status_code == 200
    assert 'TripWallet MVP UI' in response.text
