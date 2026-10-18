"""Tests for the Zaptec -> LibreBooking middleware.

LibreBooking is faked with httpx.MockTransport so tests exercise the real
HTTP client code paths (auth headers, custom attribute lookup, reservations).
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

# --- environment must exist before app import (pydantic-settings) ---
os.environ.update(
    {
        "LIBREBOOKING_URL": "https://booking.test",
        "LIBREBOOKING_USERNAME": "svc",
        "LIBREBOOKING_PASSWORD": "svcpass",
        "RFID_MAPPING": "custom_attribute",
        "RFID_CUSTOM_ATTRIBUTE_ID": "1",
        "ZAPTEC_BASIC_USERNAME": "zapuser",
        "ZAPTEC_BASIC_PASSWORD": "zappass",
        "REQUIRE_ACTIVE_RESERVATION": "true",
    }
)

BASIC_AUTH = "Basic " + base64.b64encode(b"zapuser:zappass").decode()

MATCHED_RFID = "04AABBCCDD"
UNMATCHED_RFID = "DEADBEEF"
CHARGER_ID = "b7f0c79c-0a76-4b87-a469-90d91e3ac901"
INSTALLATION_ID = "4adc8954-274b-4acd-a2e3-3a31567565e9"


def _utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S+0000")


def make_mock_lb(now: datetime, *, active: bool) -> httpx.MockTransport:
    """Fake LibreBooking: user 42 carries RFID in custom attribute 1 and
    (optionally) a reservation covering `now` on resource 12."""
    start = now - timedelta(minutes=10)
    end = now + timedelta(minutes=50)
    if not active:
        # Reservation entirely in the past, outside any sane grace window.
        start = now - timedelta(hours=3)
        end = now - timedelta(hours=2)

    users = {
        "users": [
            {
                "id": 42,
                "userName": "anton",
                "firstName": "Anton",
                "lastName": "K",
                "emailAddress": "anton@example.com",
                "phoneNumber": "+46701234567",
                "customAttributes": [{"id": 123, "label": "Zaptec RFID", "value": MATCHED_RFID}],
            },
            {
                "id": 43,
                "userName": "other",
                "firstName": "Other",
                "lastName": "Person",
                "emailAddress": "other@example.com",
                "phoneNumber": "",
                "customAttributes": [{"id": 123, "label": "Zaptec RFID", "value": "99999999"}],
            },
        ]
    }
    reservations = {
        "reservations": [
            {
                "referenceNumber": "REF123",
                "startDate": _utc(start),
                "endDate": _utc(end),
                "userId": 42,
                "resourceId": 12,
                "title": "EV charging spot",
            }
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/Authentication/Authenticate"):
            body = json.loads(request.content)
            if body.get("username") == "svc" and body.get("password") == "svcpass":
                return httpx.Response(
                    200,
                    json={
                        "sessionToken": "tok-123",
                        "sessionExpires": _utc(now + timedelta(hours=2)),
                        "userId": 1,
                        "isAuthenticated": True,
                    },
                )
            return httpx.Response(401, json={"message": "bad creds"})

        if request.headers.get("X-Booked-SessionToken") != "tok-123":
            return httpx.Response(403, json={"message": "not authenticated"})

        if path.endswith("/Users/"):
            att = request.url.params.get("att1")
            if att is not None:
                filtered = [
                    u
                    for u in users["users"]
                    if any(a["value"] == att for a in u["customAttributes"])
                ]
                return httpx.Response(200, json={"users": filtered})
            return httpx.Response(200, json=users)

        if path.endswith("/Reservations/"):
            user_id = request.url.params.get("userId")
            if user_id == "42":
                return httpx.Response(200, json=reservations)
            return httpx.Response(200, json={"reservations": []})

        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def client_for(request, monkeypatch):
    """Build a TestClient whose LibreBooking backend behaves per test marker."""
    active = getattr(request, "param", True)
    now = datetime.now(timezone.utc)

    transport = make_mock_lb(now, active=active)

    import app.main as main
    from app.librebooking import LibreBookingClient

    def build_client():
        return LibreBookingClient(
            "https://booking.test", "svc", "svcpass", transport=transport
        )

    # lifespan constructs a global client with no transport; replace it
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan_noop(app):
        main.lb_client = build_client()
        yield
        await main.lb_client.close()

    monkeypatch.setattr(main.app.router, "lifespan_context", lifespan_noop)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as c:
        yield c


def _start_payload(token: str | None = MATCHED_RFID) -> dict:
    payload = {
        "chargerId": CHARGER_ID,
        "chargerName": "P1 – Basement spot 12",
        "installationId": INSTALLATION_ID,
        "installationName": "Office Garage A",
    }
    if token:
        payload["token"] = token
    return payload


def _post_start(client, payload):
    return client.post(
        "/zaptec/session-start", json=payload, headers={"Authorization": BASIC_AUTH}
    )


def test_healthz(client_for):
    r = client_for.get("/healthz")
    assert r.status_code == 200


def test_matching_rfid_with_active_reservation_is_authorized(client_for):
    r = _post_start(client_for, _start_payload())
    assert r.status_code == 200
    body = r.json()
    assert "sessionId" in body
    # sessionId must be a UUID per Zaptec spec
    import uuid as uuid_mod

    uuid_mod.UUID(body["sessionId"])


def test_unknown_rfid_gets_401(client_for):
    r = _post_start(client_for, _start_payload(UNMATCHED_RFID))
    assert r.status_code == 401
    assert r.json()["error"] == "unknown_rfid_token"


def test_missing_rfid_token_gets_401_for_polling(client_for):
    r = _post_start(client_for, _start_payload(None))
    assert r.status_code == 401
    assert r.json()["error"] == "rfid_required"


@pytest.mark.parametrize("client_for", [False], indirect=True)
def test_matching_rfid_without_reservation_gets_401(client_for):
    r = _post_start(client_for, _start_payload())
    assert r.status_code == 401
    assert r.json()["error"] == "no_active_reservation"


def test_missing_zapcloud_auth_gets_401(client_for):
    r = client_for.post("/zaptec/session-start", json=_start_payload())
    assert r.status_code == 401


def test_rfid_case_insensitive_match(client_for):
    r = _post_start(client_for, _start_payload("04aabbccdd"))
    assert r.status_code == 200


def test_librebooking_down_returns_502(client_for, monkeypatch):
    from app.librebooking import LibreBookingError

    async def boom(*a, **k):
        raise LibreBookingError("connection refused")

    import app.main as main
    from app.librebooking import LibreBookingClient

    monkeypatch.setattr(LibreBookingClient, "find_user_by_rfid", boom)
    r = _post_start(client_for, _start_payload())
    assert r.status_code == 502


def test_session_end_accepted(client_for):
    r = client_for.post(
        "/zaptec/session-end",
        json={
            "sessionId": "b92a3549-5a36-4fab-b5c3-12e84a89c517",
            "sessionStart": "2026-10-18T09:00:00+0000",
            "sessionEnd": "2026-10-18T11:30:00+0000",
            "energy": 14.2,
        },
        headers={"Authorization": BASIC_AUTH},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
