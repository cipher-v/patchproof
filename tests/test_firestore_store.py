"""Contract tests for the transactional Firestore persistence adapter."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from patchproof.firestore_store import FirestoreVerificationRunStore
from patchproof.storage import AcceptanceKind, StoredEvidenceConflictError
from patchproof.workflow import (
    PullRequestEvent,
    RunLifecycle,
    RunTransition,
    TerminalReason,
)


class FakeSnapshot:
    def __init__(self, data=None) -> None:
        self.data = data
        self.exists = data is not None

    def get(self, key):
        return self.data[key]

    def to_dict(self):
        return dict(self.data)


class FakeDocument:
    def __init__(self, client, collection: str, document_id: str) -> None:
        self.client = client
        self.key = (collection, document_id)

    def get(self, transaction=None):
        del transaction
        return FakeSnapshot(self.client.documents.get(self.key))


class FakeQuery:
    def __init__(
        self,
        client,
        collection: str,
        field_filter=None,
        order_field=None,
        descending=False,
        maximum=None,
    ) -> None:
        self.client = client
        self.collection = collection
        self.field_filter = field_filter
        self.order_field = order_field
        self.descending = descending
        self.maximum = maximum

    def where(self, *, filter):
        return FakeQuery(
            self.client,
            self.collection,
            filter,
            self.order_field,
            self.descending,
            self.maximum,
        )

    def order_by(self, field: str, *, direction):
        return FakeQuery(
            self.client,
            self.collection,
            self.field_filter,
            field,
            "DESCENDING" in str(direction),
            self.maximum,
        )

    def limit(self, maximum: int):
        return FakeQuery(
            self.client,
            self.collection,
            self.field_filter,
            self.order_field,
            self.descending,
            maximum,
        )

    def stream(self):
        documents = []
        for (collection, _), document in self.client.documents.items():
            if collection != self.collection:
                continue
            if self.field_filter is None or document.get(self.field_filter.field_path) == (
                self.field_filter.value
            ):
                documents.append(document)
        if self.order_field is not None:
            documents.sort(
                key=lambda document: document[self.order_field],
                reverse=self.descending,
            )
        if self.maximum is not None:
            documents = documents[: self.maximum]
        yield from (FakeSnapshot(document) for document in documents)

    def document(self, document_id: str):
        return FakeDocument(self.client, self.collection, document_id)


class FakeTransaction:
    def __init__(self, client) -> None:
        self.client = client

    def create(self, reference, document):
        if reference.key in self.client.documents:
            raise AssertionError("fake create encountered an existing document")
        self.client.documents[reference.key] = dict(document)

    def set(self, reference, document):
        self.client.documents[reference.key] = dict(document)


class FakeFirestoreClient:
    def __init__(self) -> None:
        self.documents = {}

    def collection(self, name: str):
        return FakeQuery(self, name)


def build_store(now: datetime | None = None):
    client = FakeFirestoreClient()
    transaction = FakeTransaction(client)
    clock_value = now or datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    store = FirestoreVerificationRunStore(
        client=client,
        clock=lambda: clock_value,
        transaction_runner=lambda operation: operation(transaction),
    )
    return store, client


def event(*, delivery: str, head: str, updated_at: datetime) -> PullRequestEvent:
    return PullRequestEvent(
        delivery_id=delivery,
        action="synchronize",
        repository="Owner/Repo",
        pr_number=18,
        base_sha="a" * 40,
        head_sha=head * 40,
        head_updated_at=updated_at,
        title="Fix behavior",
        body="Regression details",
        installation_id=42,
    )


def test_firestore_acceptance_is_idempotent_and_supersedes_older_head() -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    store, _ = build_store(now)
    first = store.accept_pull_request(event(delivery="one", head="b", updated_at=now))
    repeated = store.accept_pull_request(event(delivery="one", head="b", updated_at=now))
    second = store.accept_pull_request(
        event(delivery="two", head="c", updated_at=now + timedelta(minutes=1))
    )

    assert first.kind is AcceptanceKind.CREATED
    assert repeated.kind is AcceptanceKind.DUPLICATE_DELIVERY
    assert repeated.run.run_id == first.run.run_id
    assert second.kind is AcceptanceKind.CREATED
    superseded = store.get_run(first.run.run_id)
    assert superseded.lifecycle is RunLifecycle.TERMINAL
    assert superseded.terminal_reason is TerminalReason.SUPERSEDED
    assert superseded.superseded_by_run_id == second.run.run_id
    assert len(store.list_runs(repository="owner/repo", pr_number=18)) == 2


def test_firestore_evidence_publication_and_failure_records_are_immutable() -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    store, _ = build_store(now)
    run = store.accept_pull_request(event(delivery="one", head="b", updated_at=now)).run
    document = '{"result":"abstained"}'
    digest = hashlib.sha256(document.encode()).hexdigest()

    stored = store.save_evidence(run_id=run.run_id, document_json=document, sha256=digest)
    assert store.save_evidence(run_id=run.run_id, document_json=document, sha256=digest) == stored
    with pytest.raises(StoredEvidenceConflictError):
        replacement = '{"result":"supported"}'
        store.save_evidence(
            run_id=run.run_id,
            document_json=replacement,
            sha256=hashlib.sha256(replacement.encode()).hexdigest(),
        )

    publication = store.begin_publication(run_id=run.run_id, payload_sha256="1" * 64)
    assert publication.attempt_count == 1
    assert store.begin_publication(run_id=run.run_id, payload_sha256="1" * 64).attempt_count == 2
    assert store.set_check_run_id(run_id=run.run_id, check_run_id=91).check_run_id == 91


def test_firestore_optimistic_transition_and_terminal_failure() -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    store, _ = build_store(now)
    run = store.accept_pull_request(event(delivery="one", head="b", updated_at=now)).run
    queued = store.transition_run(
        run_id=run.run_id,
        expected_version=run.version,
        transition=RunTransition(lifecycle=RunLifecycle.QUEUED),
    )
    failure = store.fail_run(
        run_id=run.run_id,
        error_code="WORKSPACE_FAILED",
        summary="Executor failed closed.",
        retryable=True,
        raw_response_sha256="2" * 64,
    )
    assert queued.lifecycle is RunLifecycle.QUEUED
    assert failure.retryable is True
    assert failure.raw_response_sha256 == "2" * 64
    assert store.get_failure(run.run_id) == failure
    assert store.get_run(run.run_id).terminal_reason is TerminalReason.FAILED


def test_firestore_recent_runs_are_bounded_and_newest_first() -> None:
    now = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    store, _ = build_store(now)
    first = store.accept_pull_request(event(delivery="one", head="b", updated_at=now)).run
    store.clock = lambda: now + timedelta(minutes=1)
    second = store.accept_pull_request(
        event(delivery="two", head="c", updated_at=now + timedelta(minutes=1))
    ).run

    assert [run.run_id for run in store.list_recent_runs(limit=1)] == [second.run_id]
    assert [run.run_id for run in store.list_recent_runs(limit=2)] == [
        second.run_id,
        first.run_id,
    ]
    with pytest.raises(ValueError, match="between one and eight"):
        store.list_recent_runs(limit=9)
