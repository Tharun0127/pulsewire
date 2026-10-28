"""FastAPI application factory."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.routers import auth, rooms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.session_factory = build_session_factory(engine)
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        yield
        await app.state.redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Pulsewire", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(rooms.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "instance": settings.instance_id}

    return app


app = create_app()
