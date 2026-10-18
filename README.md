# zaptec-librebooking-mw

Middleware between **Zaptec Cloud (ZapCloud)** webhooks and **LibreBooking**.

When a driver plugs in a car and taps an RFID card, ZapCloud calls this
service's *session start* webhook. The middleware looks up the RFID token in
LibreBooking and checks that the matching user has a reservation covering the
current time. If yes → charging starts (`200` + sessionId). If no → `401`, and
ZapCloud keeps polling every 10 s until the driver taps a valid card, the
5-minute timeout elapses, or the cable is unplugged.

```
EV / RFID  ->  ZapCloud  --POST /zaptec/session-start-->  this middleware
                                                        |  look up user by RFID attribute
                   200 {sessionId} <- charging starts --+  check active reservation (± grace)
                   401 {error}     <- denied ---------+
ZapCloud  --POST /zaptec/session-end (sessionId, times, energy)-->  logged for billing
```

## Zaptec webhook contract (per docs.zaptec.com)

* `POST /zaptec/session-start` — `{token?, chargerId, chargerName, installationId, installationName}`
  * `200 {"sessionId": "<uuid>"}` → authorized
  * `401 {"error", "error_description"}` → retryable auth failure (ZapCloud polls every 10 s)
  * any other 4xx/5xx → charging denied immediately
* `POST /zaptec/session-end` — `{sessionId, sessionStart, sessionEnd, energy}` → `200 {"status":"ok"}`
* ZapCloud authenticates with **HTTP Basic** (default) or **Bearer** (if you set an
  Authentication URL on the Zaptec installation). `ZAPTEC_BASIC_*` / `ZAPTEC_BEARER_TOKEN`
  configure what this middleware expects.

> ZapCloud re-calls session start **every 10 s** while waiting for the RFID scan, so
> requests without `token` must return 401 (this app does exactly that).

## LibreBooking contract

* `POST /Web/Services/index.php/Authentication/Authenticate` (no trailing slash!) with a
  service account → `sessionToken` + `userId`, then secure calls with headers
  `X-Booked-SessionToken` / `X-Booked-UserId`.
* RFID matching (`RFID_MAPPING`):
  * `custom_attribute` (default): the Zaptec RFID card code is stored in LibreBooking
    custom attribute `attN` (`RFID_CUSTOM_ATTRIBUTE_ID`). Lookup first uses the documented
    `Users/?attN=<token>` filter, then falls back to a full user scan with case-insensitive
    comparison (covers hex-case / leading-zero differences between card readers).
  * `phone`: the token is matched against the user's phoneNumber.
* Reservation check: `GET Reservations/?userId=<id>&startDateTime=now&endDateTime=now`
  with an optional grace window (`GRACE_MINUTES_BEFORE/AFTER`) so drivers can plug in
  slightly before the booking starts. Set `REQUIRE_ACTIVE_RESERVATION=false` to authorize
  any valid RFID regardless of booking.
* `CHARGER_RESOURCE_MAP` maps Zaptec `chargerId`/`installationId` UUIDs to LibreBooking
  `resourceId`s, so only bookings of the *right* spot/vehicle authorize the session.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in LibreBooking URL + service account
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
```

or

```bash
docker build -t zaptec-lb-mw .
docker run --env-file .env -p 8080:8080 zaptec-lb-mw
```

ZapCloud requires the webhook to be exposed over **HTTPS** — put it behind your
reverse proxy (nginx/traefik/cloudflared) and point the Zaptec installation's
Session start/end URLs at `https://<host>/zaptec/session-start` and
`.../session-end`.

## Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

LibreBooking is mocked with `httpx.MockTransport`, so tests cover the real HTTP
paths (authenticate, att filter, reservation window logic, 401/502 semantics).

## Manual smoke test

```bash
# unauthorized RFID -> 401
curl -s -u zapuser:zappass -X POST localhost:8080/zaptec/session-start \
  -H 'Content-Type: application/json' \
  -d '{"token":"DEADBEEF","chargerId":"b7f0c79c-0a76-4b87-a469-90d91e3ac901","installationId":"4adc8954-274b-4acd-a2e3-3a31567565e9"}'
```

## Notes / hardening ideas

* Session-end data is currently only logged; wire it to invoicing if needed.
* A small in-memory cache of RFID→user lookups can protect LibreBooking from the
  10 s ZapCloud polling burst when a card is invalid.
* If you enable `ZAPTEC_BEARER_TOKEN`, also implement a token endpoint that accepts
  ZapCloud's `grant_type=password&username=...&password=...` form post.
