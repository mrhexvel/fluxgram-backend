import asyncio
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from fluxgram_backend.application.plugin_reputation_decisions import (
    ClaimedReputationDecision,
    PluginReputationDecisionQueue,
    ReputationDecisionLeaseLost,
)
from fluxgram_backend.application.plugin_reputation_publisher import (
    PluginReputationPublisher,
    PublishedReputationRecord,
    ReputationPublicationConflict,
    ReputationPublicationExpired,
    ReputationPublicationInvalidError,
    ReputationPublicationNotCurrent,
    ReputationPublicationTransientError,
)


@dataclass(frozen=True, slots=True)
class PluginReputationWorkerConfig:
    worker_id: str
    lease_duration: timedelta
    renewal_interval: timedelta
    poll_seconds: float
    retry_initial: timedelta
    retry_maximum: timedelta
    max_attempts: int

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker ID is required")
        if self.lease_duration <= timedelta(0) or self.lease_duration > timedelta(minutes=15):
            raise ValueError("worker lease is invalid")
        if not timedelta(0) < self.renewal_interval < self.lease_duration:
            raise ValueError("worker renewal interval is invalid")
        if not math.isfinite(self.poll_seconds) or not 0 < self.poll_seconds <= 60:
            raise ValueError("worker poll interval is invalid")
        if not timedelta(0) < self.retry_initial <= self.retry_maximum <= timedelta(days=1):
            raise ValueError("worker retry interval is invalid")
        if not 1 <= self.max_attempts <= 100:
            raise ValueError("worker maximum attempts is invalid")


class PluginReputationWorkerObserver(Protocol):
    def poll_succeeded(self, claimed: bool) -> None: ...

    def lease_renewed(self) -> None: ...

    def lease_lost(self) -> None: ...

    def published(self) -> None: ...

    def failed(self, error_code: str) -> None: ...

    def retried(self) -> None: ...

    def shutdown_rescheduled(self) -> None: ...


class PluginReputationPublisherWorker:
    def __init__(
        self,
        queue: PluginReputationDecisionQueue,
        publisher: PluginReputationPublisher,
        config: PluginReputationWorkerConfig,
        observer: PluginReputationWorkerObserver | None = None,
    ) -> None:
        self._queue = queue
        self._publisher = publisher
        self._config = config
        self._observer = observer

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = await self.process_next(stop)
            if processed or stop.is_set():
                continue

            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._config.poll_seconds)

    async def process_next(self, stop: asyncio.Event | None = None) -> bool:
        claimed = await self._queue.claim_next(
            self._config.worker_id,
            self._config.lease_duration,
        )
        self._observe(lambda observer: observer.poll_succeeded(claimed is not None))
        if claimed is None:
            return False

        decision = claimed.decision
        lease_token = decision.lease_token
        if lease_token is None:
            raise RuntimeError("claimed decision has no lease token")

        if stop is not None and stop.is_set():
            await self._reschedule_shutdown(claimed, lease_token)
            return True

        try:
            result = await self._publish_with_heartbeat(claimed, lease_token)
        except ReputationDecisionLeaseLost:
            self._observe(lambda observer: observer.lease_lost())
            return True
        except asyncio.CancelledError:
            await self._reschedule_shutdown(claimed, lease_token)
            raise
        except ReputationPublicationExpired:
            await self._mark_failed(claimed, lease_token, "decision_expired")
        except ReputationPublicationNotCurrent:
            await self._mark_failed(claimed, lease_token, "decision_not_current")
        except ReputationPublicationConflict:
            await self._mark_failed(claimed, lease_token, "publication_conflict")
        except ReputationPublicationInvalidError:
            await self._mark_failed(claimed, lease_token, "signer_invalid_response")
        except ReputationPublicationTransientError:
            await self._retry(claimed, lease_token)
        else:
            await self._queue.mark_published(
                decision.decision_operation_id,
                lease_token,
                result,
            )
            self._observe(lambda observer: observer.published())

        return True

    async def _publish_with_heartbeat(
        self,
        claimed: ClaimedReputationDecision,
        lease_token: str,
    ) -> PublishedReputationRecord:
        publish = asyncio.create_task(
            self._publisher.publish(claimed.publication_context, claimed.decision.publication)
        )
        heartbeat = asyncio.create_task(self._heartbeat(claimed, lease_token))
        try:
            done, _ = await asyncio.wait((publish, heartbeat), return_when=asyncio.FIRST_COMPLETED)
            if heartbeat in done:
                error = heartbeat.exception()
                publish.cancel()
                with suppress(asyncio.CancelledError):
                    await publish
                if error is None:
                    raise RuntimeError("lease heartbeat ended unexpectedly")
                raise error

            return await publish
        finally:
            heartbeat.cancel()
            publish.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            with suppress(asyncio.CancelledError):
                await publish

    async def _heartbeat(
        self,
        claimed: ClaimedReputationDecision,
        lease_token: str,
    ) -> None:
        while True:
            await asyncio.sleep(self._config.renewal_interval.total_seconds())
            await self._queue.renew_claim(
                claimed.decision.decision_operation_id,
                lease_token,
                self._config.lease_duration,
            )
            self._observe(lambda observer: observer.lease_renewed())

    async def _retry(
        self,
        claimed: ClaimedReputationDecision,
        lease_token: str,
    ) -> None:
        decision = claimed.decision
        if decision.attempt_count >= self._config.max_attempts:
            await self._mark_failed(claimed, lease_token, "retry_exhausted")
            return

        retry_delay = self._retry_delay(decision.attempt_count)
        now_millis = await self._queue.current_time_millis()
        if now_millis + int(retry_delay.total_seconds() * 1000) >= (
            decision.publication.expires_at_millis
        ):
            await self._mark_failed(claimed, lease_token, "decision_expired")
            return

        await self._queue.reschedule(
            decision.decision_operation_id,
            lease_token,
            retry_delay,
            "signer_unavailable",
        )
        self._observe(lambda observer: observer.retried())

    async def _mark_failed(
        self,
        claimed: ClaimedReputationDecision,
        lease_token: str,
        error_code: str,
    ) -> None:
        await self._queue.mark_failed(
            claimed.decision.decision_operation_id,
            lease_token,
            error_code,
        )
        self._observe(lambda observer: observer.failed(error_code))

    async def _reschedule_shutdown(
        self,
        claimed: ClaimedReputationDecision,
        lease_token: str,
    ) -> None:
        with suppress(ReputationDecisionLeaseLost):
            await self._queue.reschedule(
                claimed.decision.decision_operation_id,
                lease_token,
                timedelta(0),
                "worker_shutdown",
            )
            self._observe(lambda observer: observer.shutdown_rescheduled())

    def _retry_delay(self, attempt_count: int) -> timedelta:
        multiplier = 2 ** min(max(attempt_count - 1, 0), 30)
        seconds = min(
            self._config.retry_initial.total_seconds() * multiplier,
            self._config.retry_maximum.total_seconds(),
        )
        return timedelta(seconds=seconds)

    def _observe(
        self,
        callback: Callable[[PluginReputationWorkerObserver], None],
    ) -> None:
        if self._observer is None:
            return
        with suppress(Exception):
            callback(self._observer)
