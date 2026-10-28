"""FastAPI application factory."""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db import build_engine, build_session_factory
from app.realtime import sweeper, ws
from app.realtime.hub import Hub, LocalBus
from app.realtime.redis_bus import RedisBus
from app.routers import auth, rooms

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def close_all_sockets(hub: Hub) -> None:
    """On shutdown (deploy, scale-in), close sockets with 1012 "service
    restart" so clients reconnect at once, via the load balancer, to another
    instance, instead of waiting for a timeout."""
    await asyncio.gather(*(conn.close(1012, "server restarting") for conn in list(hub.registry.all)))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = build_engine(settings)
        app.state.settings = settings
        app.state.session_factory = build_session_factory(engine)
        app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        hub = Hub(settings.instance_id)
        # Redis pub/sub so sockets on different instances share rooms.
        # LocalBus (single instance, no Redis hop) is kept for comparison.
        hub.bus = RedisBus(app.state.redis, hub) if settings.use_redis_bus else LocalBus(hub)
        await hub.bus.start()
        app.state.hub = hub
        sweeper_task = asyncio.create_task(sweeper.run(app.state))
        yield
        sweeper_task.cancel()
        await close_all_sockets(hub)
        await hub.bus.stop()
        await app.state.redis.aclose()
        await engine.dispose()

    app = FastAPI(title="Pulsewire", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(rooms.router)
    app.include_router(ws.router)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "instance": settings.instance_id}

    @app.get("/metrics/instance")
    async def instance_metrics() -> dict:
        """Connections and memory of THIS process, read by the load test."""
        hub: Hub = app.state.hub
        return {"instance": settings.instance_id, "connections": len(hub.registry.all),
                "rooms_subscribed": len(hub.registry.by_room), "rss_mb": rss_mb()}

    return app


def rss_mb() -> float | None:
    """Resident memory of this process from /proc (Linux only)."""
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


app = create_app()
