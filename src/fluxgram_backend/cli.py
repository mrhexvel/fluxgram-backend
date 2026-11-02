import argparse
import asyncio
import json
from collections.abc import Sequence

import uvicorn

from fluxgram_backend.api.app import create_api_app
from fluxgram_backend.api.reputation_app import create_reputation_api_app
from fluxgram_backend.bot.app import create_bot_app
from fluxgram_backend.bot.webhook import get_webhook_info, set_webhook
from fluxgram_backend.database.migrations import run_migrations
from fluxgram_backend.log_config import configure_logging
from fluxgram_backend.settings import Settings


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fluxgram-backend")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("api")
    subcommands.add_parser("reputation-api")
    subcommands.add_parser("bot")
    subcommands.add_parser("migrate")

    webhook = subcommands.add_parser("webhook")
    webhook_subcommands = webhook.add_subparsers(dest="webhook_command", required=True)
    webhook_subcommands.add_parser("set")
    webhook_subcommands.add_parser("info")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = create_parser().parse_args(argv)
    settings = Settings()
    configure_logging(settings.log_level)

    if arguments.command == "api":
        uvicorn.run(
            create_api_app(settings),
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
        )
        return 0

    if arguments.command == "reputation-api":
        uvicorn.run(
            create_reputation_api_app(settings),
            host=settings.api_host,
            port=settings.api_port,
            log_config=None,
        )
        return 0

    if arguments.command == "bot":
        uvicorn.run(
            create_bot_app(settings),
            host=settings.bot_host,
            port=settings.bot_port,
            log_config=None,
        )
        return 0

    if arguments.command == "migrate":
        run_migrations(settings)
        return 0

    if arguments.webhook_command == "set":
        webhook_set = asyncio.run(set_webhook(settings))
        print(json.dumps({"ok": webhook_set}, separators=(",", ":")))
        return 0 if webhook_set else 1

    webhook_info = asyncio.run(get_webhook_info(settings))
    print(json.dumps(webhook_info, separators=(",", ":")))
    return 0
