import asyncio
import signal
from contextlib import suppress
from datetime import timedelta

from fluxgram_backend.application.plugin_reputation_publisher import PluginReputationPublisher
from fluxgram_backend.publisher.composition import open_plugin_reputation_publisher_runtime
from fluxgram_backend.publisher.operations import PluginReputationPublisherOperations
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

    operations = PluginReputationPublisherOperations(
        worker_settings.plugin_reputation_operations_host,
        worker_settings.plugin_reputation_operations_port,
        worker_settings.plugin_reputation_readiness_stale_seconds,
        worker_settings.plugin_reputation_operations_allowed_network,
        worker_settings.plugin_reputation_operations_max_connections,
    )
    try:
        async with operations.serve(), open_plugin_reputation_publisher_runtime(
            database_url, publisher_settings
        ) as runtime:
            await runtime.publisher.check_signer()
            operations.signer_succeeded()
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
                operations,
            )
            operations.worker_started()
            worker_task = asyncio.create_task(worker.run(stop))
            probe_task = asyncio.create_task(
                _probe_signer(
                    runtime.publisher,
                    operations,
                    stop,
                    worker_settings.plugin_reputation_signer_probe_seconds,
                )
            )
            stop_wait = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait(
                    (worker_task, probe_task, stop_wait),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if worker_task in done:
                    await worker_task
                elif probe_task in done:
                    try:
                        await probe_task
                    except Exception:
                        stop.set()
                        with suppress(Exception):
                            await _finish_worker(
                                worker_task,
                                worker_settings.plugin_reputation_worker_shutdown_grace_seconds,
                            )
                        raise
                    await _finish_worker(
                        worker_task,
                        worker_settings.plugin_reputation_worker_shutdown_grace_seconds,
                    )
                else:
                    await _finish_worker(
                        worker_task,
                        worker_settings.plugin_reputation_worker_shutdown_grace_seconds,
                    )
            finally:
                stop.set()
                stop_wait.cancel()
                probe_task.cancel()
                worker_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await stop_wait
                with suppress(asyncio.CancelledError, Exception):
                    await probe_task
                with suppress(asyncio.CancelledError, Exception):
                    await worker_task
                operations.worker_stopped()
    finally:
        for signal_value in registered_signals:
            loop.remove_signal_handler(signal_value)


async def _probe_signer(
    publisher: PluginReputationPublisher,
    operations: PluginReputationPublisherOperations,
    stop: asyncio.Event,
    interval_seconds: float,
) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            return
        except TimeoutError:
            pass

        await publisher.check_signer()
        operations.signer_succeeded()


async def _finish_worker(task: asyncio.Task[None], grace_seconds: float) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), grace_seconds)
    except TimeoutError:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
