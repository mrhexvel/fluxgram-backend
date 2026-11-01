import asyncio
import sys
from typing import Any

from pydantic import ValidationError

from fluxgram_backend.log_config import configure_logging
from fluxgram_backend.publisher.runner import run_plugin_reputation_publisher
from fluxgram_backend.publisher.settings import (
    PluginReputationProcessSettings,
    PluginReputationPublisherSettings,
    PluginReputationWorkerSettings,
)


def main() -> int:
    process_type: Any = PluginReputationProcessSettings
    publisher_type: Any = PluginReputationPublisherSettings
    worker_type: Any = PluginReputationWorkerSettings
    try:
        process = process_type()
        publisher = publisher_type()
        worker = worker_type()
    except ValidationError as error:
        fields = sorted(
            {
                str(item["loc"][0]).upper()
                for item in error.errors(include_url=False, include_input=False)
                if item["loc"]
            }
        )
        print(
            "Publisher configuration is invalid. Check environment fields: "
            + ", ".join(fields),
            file=sys.stderr,
        )
        return 2

    configure_logging(process.log_level)
    asyncio.run(
        run_plugin_reputation_publisher(
            process.database_url.get_secret_value(),
            publisher,
            worker,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
