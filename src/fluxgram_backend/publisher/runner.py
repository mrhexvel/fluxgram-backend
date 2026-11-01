import asyncio
import signal
from contextlib import suppress
from datetime import timedelta

from fluxgram_backend.publisher.composition import open_plugin_reputation_publisher_runtime
from fluxgram_backend.publisher.settings import (
    PluginReputationPublisherSettings,
    PluginReputationWorkerSettings,
)
from fluxgram_backend.publisher.worker import (
    PluginReputationPublisherWorker,
    PluginReputationWorkerConfig,
)


async def run_plugin_reputation_publisher(
    database_url: str,
    publisher_settings: PluginReputationPublisherSettings,
    worker_settings: PluginReputationWorkerSettings,
) -> None:
    if (
        publisher_settings.vault_signer_timeout_seconds
        + worker_settings.plugin_reputation_worker_renewal_seconds
        >= worker_settings.plugin_reputation_worker_lease_seconds
    ):
        raise ValueError("signer timeout and renewal margin must fit within worker lease")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    for signal_value in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_value, stop.set)
        except NotImplementedError:
            continue
        registered_signals.append(signal_value)

    try:
        async with open_plugin_reputation_publisher_runtime(
            database_url,
            publisher_settings,
        ) as runtime:
            config = PluginReputationWorkerConfig(
                worker_settings.plugin_reputation_worker_id,
                timedelta(seconds=worker_settings.plugin_reputation_worker_lease_seconds),
                timedelta(seconds=worker_settings.plugin_reputation_worker_renewal_seconds),
                worker_settings.plugin_reputation_worker_poll_seconds,
                timedelta(seconds=worker_settings.plugin_reputation_worker_retry_initial_seconds),
                timedelta(seconds=worker_settings.plugin_reputation_worker_retry_max_seconds),
                worker_settings.plugin_reputation_worker_max_attempts,
            )
            worker = PluginReputationPublisherWorker(
                runtime.queue,
                runtime.publisher,
                config,
            )
            task = asyncio.create_task(worker.run(stop))
            stop_wait = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    (task, stop_wait),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if task in done:
                    await task
                else:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(task),
                            worker_settings.plugin_reputation_worker_shutdown_grace_seconds,
                        )
                    except TimeoutError:
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
            finally:
                stop_wait.cancel()
                with suppress(asyncio.CancelledError):
                    await stop_wait
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                else:
                    with suppress(asyncio.CancelledError):
                        task.exception()
    finally:
        for signal_value in registered_signals:
            loop.remove_signal_handler(signal_value)
