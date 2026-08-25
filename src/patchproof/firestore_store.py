"""Firestore implementation of PatchProof's durable verification-run store."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from patchproof.claim_agent import ModelUsage
from patchproof.storage import (
    AcceptanceKind,
    AcceptanceResult,
    CheckPublication,
    ConcurrentRunUpdateError,
    RunFailureRecord,
    RunNotFoundError,
    StoredEvidence,
    StoredEvidenceConflictError,
)
from patchproof.workflow import (
    PublicationState,
    PullRequestEvent,
    RevisionState,
    RunLifecycle,
    RunPhase,
    RunTransition,
    TerminalReason,
    VerificationRun,
    apply_run_transition,
)

_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class FirestoreVerificationRunStore:
    """Transactional Firestore store with the same semantics as the local SQLite adapter."""

    def __init__(
        self,
        *,
        client: firestore.Client,
        namespace: str = "patchproof",
        clock: Callable[[], datetime] | None = None,
        transaction_runner: Callable[[Callable[[Any], Any]], Any] | None = None,
    ) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]{1,39}", namespace) is None:
            raise ValueError("Firestore namespace must be a bounded lowercase identifier")
        self.client = client
        self.namespace = namespace
        self.clock = clock or (lambda: datetime.now(UTC))
        self.transaction_runner = transaction_runner or self._run_transaction

    def accept_pull_request(self, event: PullRequestEvent) -> AcceptanceResult:
        """Atomically deduplicate deliveries/revisions and supersede one older current run."""
        now = self._now()

        def operation(transaction) -> AcceptanceResult:
            delivery_ref = self._document("deliveries", event.delivery_id)
            delivery = delivery_ref.get(transaction=transaction)
            if delivery.exists:
                return AcceptanceResult(
                    kind=AcceptanceKind.DUPLICATE_DELIVERY,
                    run=self._get_run_in_transaction(transaction, UUID(delivery.get("run_id"))),
                )

            pull_key = self._pull_key(event.repository, event.pr_number)
            current_ref = self._document("current", pull_key)
            current_snapshot = current_ref.get(transaction=transaction)
            current = (
                self._get_run_in_transaction(transaction, UUID(current_snapshot.get("run_id")))
                if current_snapshot.exists
                else None
            )
            revision_ref = self._document(
                "revisions",
                self._revision_key(
                    event.repository, event.pr_number, event.base_sha, event.head_sha
                ),
            )
            revision_snapshot = revision_ref.get(transaction=transaction)
            duplicate = (
                self._get_run_in_transaction(transaction, UUID(revision_snapshot.get("run_id")))
                if revision_snapshot.exists
                else None
            )
            if duplicate is not None:
                duplicate_is_current = current is not None and duplicate.run_id == current.run_id
                duplicate_is_not_newer = (
                    current is not None and event.head_updated_at <= current.head_updated_at
                )
                if current is None or duplicate_is_current or duplicate_is_not_newer:
                    if event.head_updated_at > duplicate.head_updated_at:
                        duplicate = duplicate.model_copy(
                            update={
                                "head_updated_at": event.head_updated_at,
                                "event_action": event.action,
                                "title": event.title,
                                "body": event.body,
                                "installation_id": event.installation_id,
                                "updated_at": now,
                                "version": duplicate.version + 1,
                            }
                        )
                        transaction.set(
                            self._document("runs", str(duplicate.run_id)),
                            self._run_document(duplicate),
                        )
                    transaction.create(
                        delivery_ref,
                        {"run_id": str(duplicate.run_id), "received_at": now},
                    )
                    return AcceptanceResult(
                        kind=AcceptanceKind.DUPLICATE_REVISION,
                        run=duplicate,
                    )

            run_id = uuid4()
            is_stale = current is not None and event.head_updated_at <= current.head_updated_at
            if current is not None and not is_stale:
                superseded = current.model_copy(
                    update={
                        "revision_state": RevisionState.SUPERSEDED,
                        "superseded_by_run_id": run_id,
                        "lifecycle": RunLifecycle.TERMINAL,
                        "terminal_reason": (
                            current.terminal_reason
                            if current.lifecycle is RunLifecycle.TERMINAL
                            else TerminalReason.SUPERSEDED
                        ),
                        "updated_at": now,
                        "version": current.version + 1,
                    }
                )
                transaction.set(
                    self._document("runs", str(current.run_id)),
                    self._run_document(superseded),
                )

            run = VerificationRun(
                run_id=run_id,
                delivery_id=event.delivery_id,
                event_action=event.action,
                repository=event.repository,
                pr_number=event.pr_number,
                base_sha=event.base_sha,
                head_sha=event.head_sha,
                head_updated_at=event.head_updated_at,
                title=event.title,
                body=event.body,
                installation_id=event.installation_id,
                lifecycle=RunLifecycle.TERMINAL if is_stale else RunLifecycle.ACCEPTED,
                phase=RunPhase.CONTEXT,
                terminal_reason=TerminalReason.SUPERSEDED if is_stale else None,
                publication_state=PublicationState.NOT_STARTED,
                revision_state=RevisionState.SUPERSEDED if is_stale else RevisionState.CURRENT,
                superseded_by_run_id=current.run_id if is_stale and current else None,
                created_at=now,
                updated_at=now,
                version=1,
            )
            transaction.create(self._document("runs", str(run_id)), self._run_document(run))
            transaction.create(delivery_ref, {"run_id": str(run_id), "received_at": now})
            transaction.set(
                revision_ref, {"run_id": str(run_id), "head_updated_at": event.head_updated_at}
            )
            if not is_stale:
                transaction.set(current_ref, {"run_id": str(run_id), "updated_at": now})
            return AcceptanceResult(
                kind=AcceptanceKind.STALE_CREATED if is_stale else AcceptanceKind.CREATED,
                run=run,
            )

        return self.transaction_runner(operation)

    def get_run(self, run_id: UUID) -> VerificationRun:
        snapshot = self._document("runs", str(run_id)).get()
        if not snapshot.exists:
            raise RunNotFoundError(f"verification run not found: {run_id}")
        return VerificationRun.model_validate(snapshot.to_dict())

    def list_runs(self, *, repository: str, pr_number: int) -> list[VerificationRun]:
        pull_key = self._pull_key(repository.lower(), pr_number)
        snapshots = (
            self._collection("runs")
            .where(filter=FieldFilter("pull_request_key", "==", pull_key))
            .stream()
        )
        runs = [VerificationRun.model_validate(snapshot.to_dict()) for snapshot in snapshots]
        return sorted(runs, key=lambda run: (run.created_at, str(run.run_id)))

    def transition_run(
        self, *, run_id: UUID, expected_version: int, transition: RunTransition
    ) -> VerificationRun:
        now = self._now()

        def operation(transaction) -> VerificationRun:
            current = self._get_run_in_transaction(transaction, run_id)
            if current.version != expected_version:
                raise ConcurrentRunUpdateError(
                    f"expected run version {expected_version}, found {current.version}"
                )
            updated = apply_run_transition(current, transition, updated_at=now)
            transaction.set(self._document("runs", str(run_id)), self._run_document(updated))
            return updated

        return self.transaction_runner(operation)

    def save_evidence(self, *, run_id: UUID, document_json: str, sha256: str) -> StoredEvidence:
        if hashlib.sha256(document_json.encode("utf-8")).hexdigest() != sha256:
            raise ValueError("evidence SHA-256 does not match serialized evidence")
        now = self._now()

        def operation(transaction) -> StoredEvidence:
            self._get_run_in_transaction(transaction, run_id)
            reference = self._document("evidence", str(run_id))
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                stored = self._stored_evidence(snapshot.to_dict())
                if stored.sha256 != sha256 or stored.document_json != document_json:
                    raise StoredEvidenceConflictError("stored evidence cannot be overwritten")
                return stored
            stored = StoredEvidence(
                run_id=run_id,
                document_json=document_json,
                sha256=sha256,
                created_at=now,
            )
            transaction.create(reference, self._evidence_document(stored))
            return stored

        return self.transaction_runner(operation)

    def get_evidence(self, run_id: UUID) -> StoredEvidence | None:
        snapshot = self._document("evidence", str(run_id)).get()
        return self._stored_evidence(snapshot.to_dict()) if snapshot.exists else None

    def begin_publication(self, *, run_id: UUID, payload_sha256: str) -> CheckPublication:
        if _SHA256.fullmatch(payload_sha256) is None:
            raise ValueError("publication payload hash must be a lowercase SHA-256 value")
        now = self._now()

        def operation(transaction) -> CheckPublication:
            self._get_run_in_transaction(transaction, run_id)
            reference = self._document("publications", str(run_id))
            snapshot = reference.get(transaction=transaction)
            if snapshot.exists:
                current = self._publication(snapshot.to_dict())
                if current.payload_sha256 != payload_sha256:
                    raise StoredEvidenceConflictError("publication payload cannot be replaced")
                updated = CheckPublication(
                    run_id=run_id,
                    payload_sha256=payload_sha256,
                    check_run_id=current.check_run_id,
                    attempt_count=current.attempt_count + 1,
                    last_error=None,
                    updated_at=now,
                )
                transaction.set(reference, self._publication_document(updated))
                return updated
            publication = CheckPublication(
                run_id=run_id,
                payload_sha256=payload_sha256,
                check_run_id=None,
                attempt_count=1,
                last_error=None,
                updated_at=now,
            )
            transaction.create(reference, self._publication_document(publication))
            return publication

        return self.transaction_runner(operation)

    def get_publication(self, run_id: UUID) -> CheckPublication | None:
        snapshot = self._document("publications", str(run_id)).get()
        return self._publication(snapshot.to_dict()) if snapshot.exists else None

    def set_check_run_id(self, *, run_id: UUID, check_run_id: int) -> CheckPublication:
        if check_run_id <= 0:
            raise ValueError("GitHub Check run ID must be positive")
        return self._update_publication(run_id, check_run_id=check_run_id)

    def record_publication_error(self, *, run_id: UUID, error: str) -> CheckPublication:
        return self._update_publication(
            run_id,
            last_error=error.strip()[:1_000] or "GitHub Check publication failed",
        )

    def _update_publication(
        self,
        run_id: UUID,
        *,
        check_run_id: int | None = None,
        last_error: str | None = None,
    ) -> CheckPublication:
        now = self._now()

        def operation(transaction) -> CheckPublication:
            reference = self._document("publications", str(run_id))
            snapshot = reference.get(transaction=transaction)
            if not snapshot.exists:
                raise RunNotFoundError("publication attempt has not started")
            current = self._publication(snapshot.to_dict())
            if (
                check_run_id is not None
                and current.check_run_id is not None
                and current.check_run_id != check_run_id
            ):
                raise StoredEvidenceConflictError("GitHub Check identity cannot be replaced")
            updated = CheckPublication(
                run_id=run_id,
                payload_sha256=current.payload_sha256,
                check_run_id=check_run_id or current.check_run_id,
                attempt_count=current.attempt_count,
                last_error=last_error if last_error is not None else current.last_error,
                updated_at=now,
            )
            transaction.set(reference, self._publication_document(updated))
            return updated

        return self.transaction_runner(operation)

    def fail_run(
        self,
        *,
        run_id: UUID,
        error_code: str,
        summary: str,
        retryable: bool,
        model_usage: ModelUsage | None = None,
        raw_response_sha256: str | None = None,
    ) -> RunFailureRecord:
        if _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("worker failure code must be a bounded uppercase identifier")
        bounded = summary.strip()[:500]
        if not bounded:
            raise ValueError("worker failure summary must not be empty")
        if raw_response_sha256 is not None and _SHA256.fullmatch(raw_response_sha256) is None:
            raise ValueError("worker failure response hash must be lowercase SHA-256")
        now = self._now()

        def operation(transaction) -> RunFailureRecord:
            failure_ref = self._document("failures", str(run_id))
            prior = failure_ref.get(transaction=transaction)
            if prior.exists:
                return self._failure(prior.to_dict())
            current = self._get_run_in_transaction(transaction, run_id)
            updated = apply_run_transition(
                current,
                RunTransition(
                    lifecycle=RunLifecycle.TERMINAL,
                    terminal_reason=TerminalReason.FAILED,
                ),
                updated_at=now,
            )
            failure = RunFailureRecord(
                run_id=run_id,
                phase=current.phase,
                error_code=error_code,
                summary=bounded,
                retryable=retryable,
                created_at=now,
                model_usage=model_usage,
                raw_response_sha256=raw_response_sha256,
            )
            transaction.set(self._document("runs", str(run_id)), self._run_document(updated))
            transaction.create(failure_ref, self._failure_document(failure))
            return failure

        return self.transaction_runner(operation)

    def get_failure(self, run_id: UUID) -> RunFailureRecord | None:
        snapshot = self._document("failures", str(run_id)).get()
        return self._failure(snapshot.to_dict()) if snapshot.exists else None

    def _run_transaction(self, operation: Callable[[Any], Any]) -> Any:
        transaction = self.client.transaction(max_attempts=5)
        return firestore.transactional(operation)(transaction)

    def _get_run_in_transaction(self, transaction, run_id: UUID) -> VerificationRun:
        snapshot = self._document("runs", str(run_id)).get(transaction=transaction)
        if not snapshot.exists:
            raise RunNotFoundError(f"verification run not found: {run_id}")
        return VerificationRun.model_validate(snapshot.to_dict())

    def _collection(self, name: str):
        return self.client.collection(f"{self.namespace}_{name}")

    def _document(self, collection: str, document_id: str):
        return self._collection(collection).document(document_id)

    def _run_document(self, run: VerificationRun) -> dict[str, Any]:
        document = run.model_dump(mode="json")
        document["pull_request_key"] = self._pull_key(run.repository, run.pr_number)
        return document

    @staticmethod
    def _evidence_document(value: StoredEvidence) -> dict[str, Any]:
        return {
            "run_id": str(value.run_id),
            "document_json": value.document_json,
            "sha256": value.sha256,
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def _stored_evidence(document: dict[str, Any]) -> StoredEvidence:
        return StoredEvidence(
            run_id=UUID(document["run_id"]),
            document_json=document["document_json"],
            sha256=document["sha256"],
            created_at=datetime.fromisoformat(document["created_at"]),
        )

    @staticmethod
    def _publication_document(value: CheckPublication) -> dict[str, Any]:
        return {
            "run_id": str(value.run_id),
            "payload_sha256": value.payload_sha256,
            "check_run_id": value.check_run_id,
            "attempt_count": value.attempt_count,
            "last_error": value.last_error,
            "updated_at": value.updated_at.isoformat(),
        }

    @staticmethod
    def _publication(document: dict[str, Any]) -> CheckPublication:
        return CheckPublication(
            run_id=UUID(document["run_id"]),
            payload_sha256=document["payload_sha256"],
            check_run_id=document.get("check_run_id"),
            attempt_count=document["attempt_count"],
            last_error=document.get("last_error"),
            updated_at=datetime.fromisoformat(document["updated_at"]),
        )

    @staticmethod
    def _failure_document(value: RunFailureRecord) -> dict[str, Any]:
        return {
            "run_id": str(value.run_id),
            "phase": value.phase.value,
            "error_code": value.error_code,
            "summary": value.summary,
            "retryable": value.retryable,
            "created_at": value.created_at.isoformat(),
            "model_usage": value.model_usage.model_dump(mode="json")
            if value.model_usage is not None
            else None,
            "raw_response_sha256": value.raw_response_sha256,
        }

    @staticmethod
    def _failure(document: dict[str, Any]) -> RunFailureRecord:
        return RunFailureRecord(
            run_id=UUID(document["run_id"]),
            phase=RunPhase(document["phase"]),
            error_code=document["error_code"],
            summary=document["summary"],
            retryable=document["retryable"],
            created_at=datetime.fromisoformat(document["created_at"]),
            model_usage=ModelUsage.model_validate(document["model_usage"])
            if document.get("model_usage") is not None
            else None,
            raw_response_sha256=document.get("raw_response_sha256"),
        )

    @staticmethod
    def _pull_key(repository: str, pr_number: int) -> str:
        return hashlib.sha256(f"{repository.lower()}#{pr_number}".encode()).hexdigest()

    @staticmethod
    def _revision_key(repository: str, pr_number: int, base_sha: str, head_sha: str) -> str:
        value = f"{repository.lower()}#{pr_number}#{base_sha}#{head_sha}"
        return hashlib.sha256(value.encode()).hexdigest()

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("store clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)
