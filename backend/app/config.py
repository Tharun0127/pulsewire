"""All runtime settings, read from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Which server this is. Stamped on every message so the demo can show
    # a message travelling from api-1 to a user on api-2.
    instance_id: str = "api-local"

    database_url: str = "postgresql+asyncpg://pulse:pulse@localhost:5433/pulsewire"
    redis_url: str = "redis://localhost:6373/0"

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 24 * 60
    bcrypt_rounds: int = 12

    # Presence: the client pings every 15 s; a missed ping or two and the
    # TTL runs out, so a closed laptop shows offline within ~30 s.
    presence_ttl_seconds: float = 30
    # How often each instance re-checks presence for its rooms and pushes
    # changes (this is what notices TTL expiries).
    presence_sweep_seconds: float = 5
    # Close sockets that sent nothing (not even a ping) for this long. Catches
    # half-open TCP connections that never send a close frame.
    idle_timeout_seconds: float = 45
    # Typing indicator lifetime, and the server-side throttle window.
    typing_ttl_seconds: float = 5

    # Per-socket outgoing queue. A client that cannot keep up is disconnected
    # instead of making the server buffer unbounded data for it.
    send_queue_size: int = 1000

    db_pool_size: int = 10
    db_max_overflow: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
