import asyncio
from typing import Any

from fluxgram_backend.bot.webhook import ALLOWED_UPDATES, get_webhook_info, set_webhook
from fluxgram_backend.settings import Settings


class FakeWebhookClient:
    def __init__(self) -> None:
        self.url: str | None = None
        self.secret: str | None = None
        self.closed = False

    async def set(self, url: str, secret: str) -> bool:
        self.url = url
        self.secret = secret
        return True

    async def info(self) -> dict[str, Any]:
        return {"url": self.url or "", "pending_update_count": 0}

    async def close(self) -> None:
        self.closed = True


def webhook_settings() -> Settings:
    return Settings.model_validate(
        {
            "telegram_bot_token": "123456:abcdefghijklmnopqrstuvwxyzABCDEFGHI",
            "telegram_webhook_base_url": "https://example.ngrok.app",
            "telegram_webhook_secret": "webhook-secret",
        }
    )


def test_set_webhook_uses_fixed_route_and_secret() -> None:
    client = FakeWebhookClient()

    result = asyncio.run(set_webhook(webhook_settings(), client))

    assert result
    assert client.url == "https://example.ngrok.app/webhooks/telegram"
    assert client.secret == "webhook-secret"
    assert not client.closed
    assert ALLOWED_UPDATES == ["message", "callback_query"]


def test_get_webhook_info_returns_public_metadata() -> None:
    client = FakeWebhookClient()

    result = asyncio.run(get_webhook_info(webhook_settings(), client))

    assert result == {"url": "", "pending_update_count": 0}
    assert not client.closed
