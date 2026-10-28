"""Alembic environment using the async engine."""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Tests set the URL explicitly on the config; otherwise use DATABASE_URL.
url = config.attributes.get("url") or os.getenv("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
target_metadata = Base.metadata


def run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(run_sync)
    await engine.dispose()


asyncio.run(run_async())
