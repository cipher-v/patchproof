"""Integration tests for transactional SQLite workflow persistence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from patchproof.storage import (
    AcceptanceKind,
    ConcurrentRunUpdateError,
    SqliteVerificationRunStore,
)
from patchproof.workflow import (
    InvalidRunTransition,
    PullRequestEvent,
    RevisionState,
    RunLifecycle,
    RunTransition,
    TerminalReason,
)

_EVENT_TIME = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)


@dataclass
class MutableClock:
    """Deterministic clock whose value tests can advance explicitly."""

    current: datetime

    def __call__(self) -> datetime:
        return self.current


def _event(
    *,
    delivery_id: str,
    head_sha: str,
    updated_at: datetime = _EVENT_TIME,
    action: str = "synchronize",
) -> PullRequestEvent:
    return PullRequestEvent(
        delivery_id=delivery_id,
        action=action,
        repository="Owner/Repository",
        pr_number=17,
        base_sha="a" * 40,
        head_sha=head_sha,
        head_updated_at=updated_at,
    )


@pytest.fixture
def store_parts(writable_test_directory: Path):
    clock = MutableClock(datetime(2026, 8, 24, 12, 0, tzinfo=UTC))
    database_path = writable_test_directory / "workflow.db"
    return SqliteVerificationRunStore(database_path, clock=clock), clock, database_path


def test_created_run_survives_store_recreation(store_parts) -> None:
    store, _, database_path = store_parts
    accepted = store.accept_pull_request(_event(delivery_id="delivery-1", head_sha="b" * 40))

    reloaded = SqliteVerificationRunStore(database_path).get_run(accepted.run.run_id)

    assert accepted.kind is AcceptanceKind.CREATED
    assert reloaded == accepted.run
    assert reloaded.repository == "owner/repository"
    assert reloaded.lifecycle is RunLifecycle.ACCEPTED
    assert reloaded.revision_state is RevisionState.CURRENT
    assert reloaded.version == 1


def test_delivery_and_revision_idempotency_return_one_run(store_parts) -> None:
    store, clock, _ = store_parts
    event = _event(delivery_id="delivery-1", head_sha="b" * 40)
    created = store.accept_pull_request(event)

    duplicate_delivery = store.accept_pull_request(event)
    clock.current += timedelta(seconds=1)
    duplicate_revision = store.accept_pull_request(
        _event(
            delivery_id="delivery-2",
            head_sha="b" * 40,
            updated_at=_EVENT_TIME + timedelta(seconds=1),
            action="reopened",
        )
    )

    assert duplicate_delivery.kind is AcceptanceKind.DUPLICATE_DELIVERY
    assert duplicate_revision.kind is AcceptanceKind.DUPLICATE_REVISION
    assert duplicate_delivery.run.run_id == duplicate_revision.run.run_id == created.run.run_id
    assert duplicate_revision.run.event_action == "reopened"
    assert duplicate_revision.run.version == 2
    assert len(store.list_runs(repository="OWNER/REPOSITORY", pr_number=17)) == 1


def test_concurrent_revision_replays_create_exactly_one_run(store_parts) -> None:
    store, _, _ = store_parts
    barrier = Barrier(2)

    def accept(delivery_id: str):
        barrier.wait()
        return store.accept_pull_request(_event(delivery_id=delivery_id, head_sha="b" * 40))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, ["delivery-1", "delivery-2"]))

    assert {result.kind for result in results} == {
        AcceptanceKind.CREATED,
        AcceptanceKind.DUPLICATE_REVISION,
    }
    assert len({result.run.run_id for result in results}) == 1
    assert len(store.list_runs(repository="owner/repository", pr_number=17)) == 1


def test_newer_head_supersedes_active_run_and_keeps_both_auditable(store_parts) -> None:
    store, clock, _ = store_parts
    old = store.accept_pull_request(_event(delivery_id="delivery-old", head_sha="b" * 40)).run
    clock.current += timedelta(seconds=1)
    new = store.accept_pull_request(
        _event(
            delivery_id="delivery-new",
            head_sha="c" * 40,
            updated_at=_EVENT_TIME + timedelta(minutes=1),
        )
    ).run

    superseded = store.get_run(old.run_id)
    runs = store.list_runs(repository="owner/repository", pr_number=17)

    assert superseded.lifecycle is RunLifecycle.TERMINAL
    assert superseded.terminal_reason is TerminalReason.SUPERSEDED
    assert superseded.revision_state is RevisionState.SUPERSEDED
    assert superseded.superseded_by_run_id == new.run_id
    assert new.lifecycle is RunLifecycle.ACCEPTED
    assert new.revision_state is RevisionState.CURRENT
    assert len(runs) == 2


def test_completed_run_keeps_its_terminal_reason_after_new_head(store_parts) -> None:
    store, clock, _ = store_parts
    old = store.accept_pull_request(_event(delivery_id="delivery-old", head_sha="b" * 40)).run
    completed = store.transition_run(
        run_id=old.run_id,
        expected_version=old.version,
        transition=RunTransition(
            lifecycle=RunLifecycle.TERMINAL,
            terminal_reason=TerminalReason.COMPLETED,
        ),
    )
    clock.current += timedelta(seconds=1)
    store.accept_pull_request(
        _event(
            delivery_id="delivery-new",
            head_sha="c" * 40,
            updated_at=_EVENT_TIME + timedelta(minutes=1),
        )
    )

    superseded = store.get_run(old.run_id)

    assert completed.terminal_reason is TerminalReason.COMPLETED
    assert superseded.terminal_reason is TerminalReason.COMPLETED
    assert superseded.revision_state is RevisionState.SUPERSEDED
    assert superseded.superseded_by_run_id is not None


def test_out_of_order_head_is_recorded_stale_without_displacing_current(store_parts) -> None:
    store, clock, _ = store_parts
    current = store.accept_pull_request(
        _event(
            delivery_id="delivery-new",
            head_sha="c" * 40,
            updated_at=_EVENT_TIME + timedelta(minutes=2),
        )
    ).run
    clock.current += timedelta(seconds=1)
    stale_result = store.accept_pull_request(
        _event(
            delivery_id="delivery-old",
            head_sha="b" * 40,
            updated_at=_EVENT_TIME + timedelta(minutes=1),
        )
    )

    assert stale_result.kind is AcceptanceKind.STALE_CREATED
    assert stale_result.run.lifecycle is RunLifecycle.TERMINAL
    assert stale_result.run.terminal_reason is TerminalReason.SUPERSEDED
    assert stale_result.run.revision_state is RevisionState.SUPERSEDED
    assert stale_result.run.superseded_by_run_id == current.run_id
    assert store.get_run(current.run_id).revision_state is RevisionState.CURRENT
    with pytest.raises(InvalidRunTransition, match="superseded"):
        store.transition_run(
            run_id=stale_result.run.run_id,
            expected_version=stale_result.run.version,
            transition=RunTransition(lifecycle=RunLifecycle.TERMINAL),
        )


def test_later_return_to_a_previously_seen_sha_creates_a_new_current_occurrence(
    store_parts,
) -> None:
    store, clock, _ = store_parts
    first = store.accept_pull_request(_event(delivery_id="delivery-first", head_sha="b" * 40)).run
    clock.current += timedelta(seconds=1)
    middle = store.accept_pull_request(
        _event(
            delivery_id="delivery-middle",
            head_sha="c" * 40,
            updated_at=_EVENT_TIME + timedelta(minutes=1),
        )
    ).run
    clock.current += timedelta(seconds=1)

    returned = store.accept_pull_request(
        _event(
            delivery_id="delivery-returned",
            head_sha="b" * 40,
            updated_at=_EVENT_TIME + timedelta(minutes=2),
        )
    )
    runs = store.list_runs(repository="owner/repository", pr_number=17)

    assert returned.kind is AcceptanceKind.CREATED
    assert returned.run.run_id not in {first.run_id, middle.run_id}
    assert returned.run.revision_state is RevisionState.CURRENT
    assert store.get_run(middle.run_id).superseded_by_run_id == returned.run.run_id
    assert len(runs) == 3


def test_store_uses_optimistic_versions_for_state_updates(store_parts) -> None:
    store, _, _ = store_parts
    run = store.accept_pull_request(_event(delivery_id="delivery-1", head_sha="b" * 40)).run
    queued = store.transition_run(
        run_id=run.run_id,
        expected_version=1,
        transition=RunTransition(lifecycle=RunLifecycle.QUEUED),
    )

    assert queued.version == 2
    with pytest.raises(ConcurrentRunUpdateError, match="expected 1"):
        store.transition_run(
            run_id=run.run_id,
            expected_version=1,
            transition=RunTransition(lifecycle=RunLifecycle.QUEUED),
        )
