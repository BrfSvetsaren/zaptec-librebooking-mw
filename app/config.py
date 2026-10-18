from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- LibreBooking ---
    librebooking_url: str = Field(
        description="Base URL of the LibreBooking installation, e.g. https://booking.example.com"
    )
    librebooking_username: str = Field(
        description="Service account username used to query the LibreBooking API"
    )
    librebooking_password: str = Field(description="Service account password")

    # How the RFID token is stored on a LibreBooking user.
    # "custom_attribute": match users/<attr_id> attribute value against the token.
    # "phone": match the user's phoneNumber against the token.
    rfid_mapping: str = Field(default="custom_attribute")
    rfid_custom_attribute_id: int = Field(
        default=1,
        description="Custom attribute number (att1, att2, ...) holding the Zaptec RFID token",
    )

    # Authorization window around a reservation (allow plug-in slightly early/late)
    grace_minutes_before: int = Field(default=15)
    grace_minutes_after: int = Field(default=60)

    # Optional mapping Zaptec chargerId (or installationId) -> LibreBooking resourceId.
    # Format: "charger-uuid:12,installation-uuid:34". If empty, any current reservation
    # by the matched user counts (no resource check).
    charger_resource_map: str = Field(default="")

    # Require the matched user to hold a current reservation, or just a valid RFID?
    require_active_reservation: bool = Field(default=True)

    # --- Zaptec-facing auth (we are the webhook target) ---
    # ZapCloud sends HTTP Basic (username/password from the auth payload) unless an
    # Authentication URL is configured, then Bearer. Set one of these to enforce it.
    zaptec_basic_username: str = Field(default="")
    zaptec_basic_password: str = Field(default="")
    zaptec_bearer_token: str = Field(default="")

    # Restrict which Zaptec installations this middleware answers for (comma-separated UUIDs)
    allowed_installation_ids: str = Field(default="")

    log_level: str = Field(default="INFO")


@lru_cache
def get_settings() -> Settings:
    return Settings()
