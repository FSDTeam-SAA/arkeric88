from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    openai_api_key: str
    google_api_key :str
    # Viator Partner API (Full access). Optional: without a key, itineraries are
    # built exactly as before and no Viator call is made. Never commit the key.
    viator_api_key: str = ""
    viator_partner_id: str = ""
    viator_base_url: str = "https://api.viator.com/partner"  # sandbox: https://api.sandbox.viator.com/partner
    # LiteAPI/Nuitee Connect is optional.  It is deliberately disabled until a
    # server-side key is configured; browser clients never receive this key.
    liteapi_api_key: str = ""
    liteapi_enabled: bool = False
    liteapi_base_url: str = "https://api.liteapi.travel/v3.0"
    liteapi_booking_base_url: str = "https://book.liteapi.travel/v3.0"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )

settings = Settings()
