import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from fluxgram_backend.application.plugin_reputation import SignedReputationRecord
from fluxgram_backend.application.plugin_reputation_decisions import (
    ClaimedReputationDecision,
    ReputationDecision,
    ReputationDecisionState,
)
from fluxgram_backend.application.plugin_reputation_publisher import (
    PublishedReputationRecord,
    ReputationPublication,
    ReputationPublicationExpired,
    ReputationPublicationInvalidError,
    ReputationPublicationNotCurrent,
    ReputationPublicationTransientError,
)
from fluxgram_backend.domain.operations import OperationContext
from fluxgram_backend.publisher.worker import (
    PluginReputationPublisherWorker,
    PluginReputationWorkerConfig,
)
from fluxgram_backend.reputation.protocol import ArchiveStatus, KeyStatus, ReputationQuery

NOW_MILLIS = 1_800_000_000_000


class FakeQueue:
    def __init__(self, claimed: ClaimedReputationDecision | None) -> None:
        self.claimed = claimed
        self.calls: list[tuple[Any, ...]] = []

    async def claim_next(
        self,
        worker_id: str,
        lease: timedelta,
    ) -> ClaimedReputationDecision | None:
        self.calls.append(("claim", worker_id, lease))
        claimed = self.claimed
        self.claimed = None
        return claimed

    async def renew_claim(
        self,
        decision_id: str,
        lease_token: str,
        lease: timedelta,
    ) -> ClaimedReputationDecision:
        self.calls.append(("renew", decision_id, lease_token, lease))
        return cast(ClaimedReputationDecision, object())

    async def current_time_millis(self) -> int:
        self.calls.append(("clock",))
        return NOW_MILLIS

    async def mark_published(
        self,
        decision_id: str,
        lease_token: str,
        result: PublishedReputationRecord,
    ) -> ReputationDecision:
        self.calls.append(("published", decision_id, lease_token, result))
        return cast(ReputationDecision, object())

    async def mark_failed(
        self,
        decision_id: str,
        lease_token: str,
        error_code: str,
    ) -> ReputationDecision:
        self.calls.append(("failed", decision_id, lease_token, error_code))
        return cast(ReputationDecision, object())

    async def reschedule(
        self,
        decision_id: str,
        lease_token: str,
        delay: timedelta,
        error_code: str,
    ) -> ReputationDecision:
        self.calls.append(("reschedule", decision_id, lease_token, delay, error_code))
        return cast(ReputationDecision, object())


class FakePublisher:
    def __init__(self, outcome: object | BaseException) -> None:
        self.outcome = outcome
        self.calls: list[tuple[OperationContext, ReputationPublication]] = []

    async def publish(
        self,
        context: OperationContext,
        publication: ReputationPublication,
    ) -> PublishedReputationRecord:
        self.calls.append((context, publication))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return cast(PublishedReputationRecord, self.outcome)


class BlockingPublisher(FakePublisher):
    def __init__(self) -> None:
        super().__init__(_result())
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False

    async def publish(
        self,
        context: OperationContext,
        publication: ReputationPublication,
    ) -> PublishedReputationRecord:
        self.calls.append((context, publication))
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return _result()


def _result() -> PublishedReputationRecord:
    return PublishedReputationRecord("publication", 0, SignedReputationRecord(b"r", b"e"))


def _claimed(
    *,
    issued_at: int = NOW_MILLIS - 1_000,
    expires_at: int = NOW_MILLIS + 60_000,
    attempt_count: int = 1,
) -> ClaimedReputationDecision:
    publication = ReputationPublication(
        ReputationQuery("a" * 64),
        ArchiveStatus.CLEAR,
        KeyStatus.NOT_APPLICABLE,
        issued_at,
        expires_at,
    )
    return ClaimedReputationDecision(
        ReputationDecision(
            1,
            "decision",
            "publication",
            publication,
            ReputationDecisionState.CLAIMED,
            datetime.now(UTC),
            attempt_count,
            "lease",
            "worker",
            datetime.now(UTC) + timedelta(seconds=30),
            None,
            None,
            None,
        )
    )


def _config(**values: Any) -> PluginReputationWorkerConfig:
    defaults = {
        "worker_id": "worker-1",
        "lease_duration": timedelta(seconds=30),
        "renewal_interval": timedelta(milliseconds=10),
        "poll_seconds": 0.01,
        "retry_initial": timedelta(seconds=2),
        "retry_maximum": timedelta(seconds=8),
        "max_attempts": 3,
    }
    defaults.update(values)
    return PluginReputationWorkerConfig(**defaults)


def _worker(
    queue: FakeQueue,
    publisher: FakePublisher,
    **config: Any,
) -> PluginReputationPublisherWorker:
    return PluginReputationPublisherWorker(
        cast(Any, queue),
        cast(Any, publisher),
        _config(**config),
    )


@pytest.mark.anyio
async def test_worker_publishes_claimed_decision() -> None:
    queue = FakeQueue(_claimed())
    publisher = FakePublisher(_result())

    assert await _worker(queue, publisher).process_next()

    assert len(publisher.calls) == 1
    assert publisher.calls[0][0].operation_id == "publication"
    assert [call[0] for call in queue.calls] == ["claim", "published"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        (ReputationPublicationExpired("expired"), "decision_expired"),
        (ReputationPublicationNotCurrent("future"), "decision_not_current"),
    ],
)
async def test_worker_rejects_non_current_decision(
    error: BaseException,
    error_code: str,
) -> None:
    queue = FakeQueue(_claimed())
    publisher = FakePublisher(error)

    assert await _worker(queue, publisher).process_next()

    assert len(publisher.calls) == 1
    assert queue.calls[-1] == ("failed", "decision", "lease", error_code)


@pytest.mark.anyio
async def test_worker_reschedules_transient_error_with_bounded_backoff() -> None:
    queue = FakeQueue(_claimed(attempt_count=2))
    publisher = FakePublisher(ReputationPublicationTransientError("unavailable"))

    assert await _worker(queue, publisher).process_next()

    assert queue.calls[-1] == (
        "reschedule",
        "decision",
        "lease",
        timedelta(seconds=4),
        "signer_unavailable",
    )


@pytest.mark.anyio
async def test_worker_exhausts_retries_and_rejects_invalid_signature() -> None:
    exhausted = FakeQueue(_claimed(attempt_count=3))
    invalid = FakeQueue(_claimed())

    await _worker(
        exhausted,
        FakePublisher(ReputationPublicationTransientError("unavailable")),
    ).process_next()
    await _worker(
        invalid,
        FakePublisher(ReputationPublicationInvalidError("invalid")),
    ).process_next()

    assert exhausted.calls[-1] == ("failed", "decision", "lease", "retry_exhausted")
    assert invalid.calls[-1] == (
        "failed",
        "decision",
        "lease",
        "signer_invalid_response",
    )


@pytest.mark.anyio
async def test_worker_does_not_hide_programming_error() -> None:
    queue = FakeQueue(_claimed())

    with pytest.raises(RuntimeError, match="bug"):
        await _worker(queue, FakePublisher(RuntimeError("bug"))).process_next()

    assert [call[0] for call in queue.calls] == ["claim"]


@pytest.mark.anyio
async def test_worker_renews_lease_during_signing() -> None:
    queue = FakeQueue(_claimed())
    publisher = BlockingPublisher()
    task = asyncio.create_task(_worker(queue, publisher).process_next())
    await publisher.started.wait()
    await asyncio.sleep(0.025)
    publisher.release.set()

    assert await task
    assert "renew" in [call[0] for call in queue.calls]
    assert queue.calls[-1][0] == "published"


@pytest.mark.anyio
async def test_worker_cancellation_reschedules_claim() -> None:
    queue = FakeQueue(_claimed())
    publisher = BlockingPublisher()
    task = asyncio.create_task(_worker(queue, publisher).process_next())
    await publisher.started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert publisher.cancelled
    assert queue.calls[-1] == (
        "reschedule",
        "decision",
        "lease",
        timedelta(0),
        "worker_shutdown",
    )
