"""Zaptec <-> LibreBooking authorization middleware.

ZapCloud (Zaptec Cloud) calls:

  POST /zaptec/session-start  {token, chargerId, chargerName, installationId, installationName}
      -> 200 {"sessionId": "<uuid>"}   charging allowed
      -> 401 {"error": ...}            not authorized (ZapCloud polls every 10s)
      -> 5xx                            hard error, ZapCloud denies charging

  POST /zaptec/session-end    {sessionId, sessionStart, sessionEnd, energy}
      -> 200 {"status": "ok"}

Flow for session start:
  1. Verify the request really comes from ZapCloud (Basic or Bearer, per config).
  2. If no RFID token in payload -> 401 (driver hasn't scanned yet; ZapCloud retries).
  3. Look up the LibreBooking user whose RFID attribute matches the token.
  4. If require_active_reservation: verify the user has a reservation covering now
     (with grace window), optionally restricted to the mapped LibreBooking resource.
  5. Match -> mint a UUID sessionId and return 200. No match -> 401.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .config import get_settings
from .librebooking import LBReservation, LBUser, LibreBookingClient, LibreBookingError

logger = logging.getLogger("zaptec-mw")

settings = get_settings()
lb_client: LibreBookingClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global lb_client
    lb_client = LibreBookingClient(
        settings.librebooking_url,
        settings.librebooking_username,
        settings.librebooking_password,
    )
    logger.info("Middleware started against LibreBooking at %s", settings.librebooking_url)
    try:
        yield
    finally:
        await lb_client.close()


app = FastAPI(
    title="Zaptec -> LibreBooking charging authorization middleware",
    lifespan=lifespan,
)


def get_lb_client() -> LibreBookingClient:
    assert lb_client is not None
    return lb_client


# --- ZapCloud-facing authentication ---

def verify_zapcloud(request: Request) -> None:
    """ZapCloud authenticates webhook calls with Basic (default) or Bearer
    (if an Authentication URL is configured on the installation)."""
    auth = request.headers.get("Authorization", "")
    if settings.zaptec_bearer_token:
        expected = f"Bearer {settings.zaptec_bearer_token}"
        if not secrets.compare_digest(auth, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return
    if settings.zaptec_basic_username:
        scheme, _, credential = auth.partition(" ")
        if scheme.lower() != "basic":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        import base64

        try:
            username, _, password = base64.b64decode(credential).decode().partition(":")
        except Exception:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        if not (
            secrets.compare_digest(username, settings.zaptec_basic_username)
            and secrets.compare_digest(password, settings.zaptec_basic_password)
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return
    # No zaptec_* credentials configured -> middleware is assumed to sit behind
    # a reverse proxy / mTLS / IP allowlist. Warn loudly in logs.
    logger.warning("ZapCloud request authentication is DISABLED (no credentials configured)")


def _installation_allowed(installation_id: str | None) -> bool:
    if not settings.allowed_installation_ids:
        return True
    allowed = {s.strip().lower() for s in settings.allowed_installation_ids.split(",") if s.strip()}
    return (installation_id or "").lower() in allowed


def _resource_for_charger(charger_id: str | None, installation_id: str | None) -> int | None:
    """charger_resource_map: 'charger-uuid:12,installation-uuid:34' -> LibreBooking resourceId."""
    for pair in settings.charger_resource_map.split(","):
        key, _, value = pair.strip().partition(":")
        if not key or not value:
            continue
        if key.lower() in {(charger_id or "").lower(), (installation_id or "").lower()}:
            try:
                return int(value)
            except ValueError:
                logger.error("Bad charger_resource_map entry: %r", pair)
    return None


def _unauthorized(reason: str, description: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": reason, "error_description": description},
    )


# --- Endpoints ---

@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post("/zaptec/session-start")
async def session_start(request: Request) -> JSONResponse:
    verify_zapcloud(request)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    token = (payload.get("token") or "").strip()
    charger_id = payload.get("chargerId")
    installation_id = payload.get("installationId")
    logger.info(
        "session-start: charger=%s installation=%s token=%s",
        charger_id,
        installation_id,
        token or "<none>",
    )

    if not _installation_allowed(installation_id):
        logger.warning("Rejected unknown installation %s", installation_id)
        return _unauthorized(
            "unknown_installation", "This charger installation is not served by this middleware."
        )

    # No RFID scanned yet -> keep answering 401 so ZapCloud keeps polling
    # until the driver taps the card (per Zaptec webhook spec).
    if not token:
        return _unauthorized(
            "rfid_required", "No RFID token in request. Driver must scan RFID card."
        )

    lb = get_lb_client()
    try:
        user: LBUser | None = await lb.find_user_by_rfid(
            token, settings.rfid_mapping, settings.rfid_custom_attribute_id
        )
    except LibreBookingError:
        logger.exception("LibreBooking lookup failed")
        # 5xx => ZapCloud denies charging immediately (not a retryable auth failure)
        raise HTTPException(
            status_code=502,
            detail={
                "error": "librebooking_unavailable",
                "error_description": "Could not reach LibreBooking.",
            },
        )

    if user is None:
        logger.info("No LibreBooking user matches RFID token %s", token)
        return _unauthorized(
            "unknown_rfid_token", "RFID token does not match any user in LibreBooking."
        )

    reservation: LBReservation | None = None
    if settings.require_active_reservation:
        now = datetime.now(timezone.utc)
        resource_id = _resource_for_charger(charger_id, installation_id)
        reservation = await lb.has_active_reservation(
            user_id=user.id,
            when=now,
            grace_before=settings.grace_minutes_before,
            grace_after=settings.grace_minutes_after,
            resource_id=resource_id,
        )
        if reservation is None:
            logger.info(
                "User %s (id=%s) has no active reservation for charger %s",
                user.username,
                user.id,
                charger_id,
            )
            return _unauthorized(
                "no_active_reservation",
                "RFID matches a user but they have no current booking covering this time/charger.",
            )

    session_id = str(uuid.uuid4())
    logger.info(
        "Authorized session %s for user %s (id=%s) reservation=%s",
        session_id,
        user.username,
        user.id,
        reservation.reference_number if reservation else "-",
    )
    return JSONResponse(status_code=200, content={"sessionId": session_id})


@app.post("/zaptec/session-end")
async def session_end(request: Request) -> JSONResponse:
    verify_zapcloud(request)

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ZapCloud only inspects the status code here; log the completed session
    # for billing/reporting. Keep the handler idempotent: ZapCloud retries on failure.
    logger.info(
        "session-end: sessionId=%s start=%s end=%s energy=%s kWh",
        payload.get("sessionId"),
        payload.get("sessionStart"),
        payload.get("sessionEnd"),
        payload.get("energy"),
    )
    return JSONResponse(status_code=200, content={"status": "ok"})
