"""Thin async client for the LibreBooking Web API.

Auth flow (per LibreBooking docs):
  POST /Web/Services/index.php/Authentication/Authenticate   (NO trailing slash!)
  -> {sessionToken, userId}
Secure calls then need headers X-Booked-SessionToken and X-Booked-UserId.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

API_BASE = "/Web/Services/index.php"


@dataclass
class LBUser:
    id: int
    username: str
    first_name: str
    last_name: str
    email: str
    phone: str
    custom_attributes: dict[int, str]


@dataclass
class LBReservation:
    reference_number: str
    start: datetime
    end: datetime
    user_id: int | None
    resource_id: int | None
    title: str


class LibreBookingError(Exception):
    pass


class LibreBookingClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._user_id: int | None = None
        self._token_expiry: float = 0.0
        self._client = httpx.AsyncClient(
            base_url=self._base,
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _authenticate(self) -> None:
        # NOTE: route must NOT have a trailing slash or LibreBooking fails.
        resp = await self._client.post(
            f"{API_BASE}/Authentication/Authenticate",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise LibreBookingError(
                f"LibreBooking authentication failed: {resp.status_code} {resp.text}"
            )
        data = resp.json()
        if not data.get("isAuthenticated"):
            raise LibreBookingError("LibreBooking rejected service account credentials")
        self._token = data["sessionToken"]
        self._user_id = data["userId"]
        try:
            expires = datetime.strptime(
                data["sessionExpires"], "%Y-%m-%dT%H:%M:%S%z"
            )
            # Refresh 60s early
            self._token_expiry = expires.timestamp() - 60
        except (KeyError, ValueError):
            self._token_expiry = time.time() + 3600

    async def _ensure_auth(self) -> None:
        if self._token is None or time.time() >= self._token_expiry:
            await self._authenticate()

    def _auth_headers(self) -> dict[str, str]:
        assert self._token is not None and self._user_id is not None
        return {
            "X-Booked-SessionToken": self._token,
            "X-Booked-UserId": str(self._user_id),
        }

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._ensure_auth()
        resp = await self._client.get(
            f"{API_BASE}/{path}", params=params, headers=self._auth_headers()
        )
        if resp.status_code in (401, 403):
            # Token expired mid-life: re-auth once, then retry.
            await self._authenticate()
            resp = await self._client.get(
                f"{API_BASE}/{path}", params=params, headers=self._auth_headers()
            )
        if resp.status_code != 200:
            raise LibreBookingError(
                f"LibreBooking GET {path} failed: {resp.status_code} {resp.text}"
            )
        return resp.json()

    # --- Users ---

    async def find_user_by_rfid(self, rfid: str, mapping: str, attr_id: int) -> LBUser | None:
        """Locate the LibreBooking user whose RFID token matches `rfid`.

        First try the documented custom-attribute filter (?attN=value); fall back to
        scanning all users for a case-insensitive match (also supports phone mapping).
        """
        token = rfid.strip().upper()

        if mapping == "custom_attribute":
            data = await self._get("Users/", params={f"att{attr_id}": rfid})
            users = data.get("users", [])
            if users:
                return _to_user(users[0])
        elif mapping == "phone":
            data = await self._get("Users/", params={"phone": rfid})
            users = data.get("users", [])
            if users:
                return _to_user(users[0])

        # Fallback: full scan + local comparison (covers formatting differences
        # like leading zeros / hex casing on RFID codes). Compare against all
        # custom attribute values, not just the configured attribute id, since
        # LibreBooking exposes the attribute's DB id (not the attN index) here.
        data = await self._get("Users/")
        for raw in data.get("users", []):
            user = _to_user(raw)
            if mapping == "custom_attribute":
                if any(
                    value and value.strip().upper() == token
                    for value in user.custom_attributes.values()
                ):
                    return user
            elif mapping == "phone":
                digits = "".join(ch for ch in user.phone if ch.isdigit())
                if digits and digits == "".join(ch for ch in token if ch.isdigit()):
                    return user
        return None

    # --- Reservations ---

    async def get_reservations_for_user(
        self, user_id: int, start: datetime, end: datetime, resource_id: int | None = None
    ) -> list[LBReservation]:
        params: dict = {
            "userId": user_id,
            "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        if resource_id is not None:
            params["resourceId"] = resource_id
        data = await self._get("Reservations/", params=params)
        reservations = []
        for raw in data.get("reservations", []):
            try:
                reservations.append(
                    LBReservation(
                        reference_number=raw.get("referenceNumber", ""),
                        start=_parse_dt(raw["startDate"]),
                        end=_parse_dt(raw["endDate"]),
                        user_id=raw.get("userId"),
                        resource_id=raw.get("resourceId"),
                        title=raw.get("title", ""),
                    )
                )
            except (KeyError, ValueError):
                logger.warning("Skipping unparseable reservation: %r", raw)
        return reservations

    async def has_active_reservation(
        self,
        user_id: int,
        when: datetime,
        grace_before: int,
        grace_after: int,
        resource_id: int | None = None,
    ) -> LBReservation | None:
        """Return the reservation covering `when` (±grace), or None."""
        window_start = when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when
        reservations = await self.get_reservations_for_user(
            user_id, window_start, window_start, resource_id
        )
        for res in reservations:
            allowed_from = res.start - timedelta(minutes=grace_before)
            allowed_until = res.end + timedelta(minutes=grace_after)
            if allowed_from <= window_start <= allowed_until:
                return res
        return None


def _to_user(raw: dict) -> LBUser:
    attrs = {}
    for attr in raw.get("customAttributes") or []:
        attrs[int(attr["id"])] = str(attr.get("value", ""))
    return LBUser(
        id=int(raw["id"]),
        username=raw.get("userName", ""),
        first_name=raw.get("firstName", ""),
        last_name=raw.get("lastName", ""),
        email=raw.get("emailAddress", ""),
        phone=raw.get("phoneNumber", "") or "",
        custom_attributes=attrs,
    )


def _parse_dt(value: str) -> datetime:
    # LibreBooking returns e.g. 2021-03-08T09:56:04+0000
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
