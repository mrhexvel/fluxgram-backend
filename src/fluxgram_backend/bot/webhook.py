from typing import Any, Protocol

from aiogram import Bot

from fluxgram_backend.settings import Settings

ALLOWED_UPDATES = ["message", "callback_query"]


class WebhookClient(Protocol):
    async def set(self, url: str, secret: str) -> bool: ...

    async def info(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class AiogramWebhookClient:
    def __init__(self, token: str) -> None:
        self._bot = Bot(token)

    async def set(self, url: str, secret: str) -> bool:
        return await self._bot.set_webhook(
            url=url,
            secret_token=secret,
            allowed_updates=ALLOWED_UPDATES,
        )

    async def info(self) -> dict[str, Any]:
        webhook_info = await self._bot.get_webhook_info()
        return webhook_info.model_dump(mode="json", exclude_none=True)

    async def close(self) -> None:
        await self._bot.session.close()


async def set_webhook(
    settings: Settings,
    client: WebhookClient | None = None,
) -> bool:
    resolved_client = client or AiogramWebhookClient(settings.require_bot_token())

    try:
        return await resolved_client.set(
            settings.webhook_url(),
            settings.require_webhook_secret(),
        )
    finally:
        if client is None:
            await resolved_client.close()


async def get_webhook_info(
    settings: Settings,
    client: WebhookClient | None = None,
) -> dict[str, Any]:
    resolved_client = client or AiogramWebhookClient(settings.require_bot_token())

    try:
        return await resolved_client.info()
    finally:
        if client is None:
            await resolved_client.close()
