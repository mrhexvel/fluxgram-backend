import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from fluxgram_backend.database.connection import Database
from fluxgram_backend.database.migrations import run_migrations
from fluxgram_backend.settings import Settings


@pytest.mark.integration
def test_postgres_readiness() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("TEST_DATABASE_URL is not configured")

    settings = Settings.model_validate({"database_url": database_url})
    run_migrations(settings)
    run_migrations(settings)

    async def check() -> None:
        database = Database(database_url)
        engine = create_async_engine(database_url)

        try:
            assert await database.is_ready()

            async with engine.connect() as connection:
                version = await connection.scalar(text("SHOW server_version_num"))

            assert int(version) // 10000 == 18
        finally:
            await engine.dispose()
            await database.close()

    asyncio.run(check())
