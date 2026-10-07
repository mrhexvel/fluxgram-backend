import asyncio

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class Database:
    def __init__(self, url: str, readiness_timeout: float = 2.0) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args={
                "command_timeout": readiness_timeout,
                "timeout": readiness_timeout,
            },
        )
        self._readiness_timeout = readiness_timeout

    async def is_ready(self) -> bool:
        try:
            async with asyncio.timeout(self._readiness_timeout):
                async with self._engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except (TimeoutError, SQLAlchemyError):
            logger.warning("PostgreSQL readiness check failed")
            return False

        return True

    async def close(self) -> None:
        await self._engine.dispose()
