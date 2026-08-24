"""Persistent workflow-store abstraction and local SQLite implementation."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

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


class AcceptanceKind(StrEnum):
    """Idempotency/supersession result of accepting one webhook event."""

    CREATED = "CREATED"
    DUPLICATE_DELIVERY = "DUPLICATE_DELIVERY"
    DUPLICATE_REVISION = "DUPLICATE_REVISION"
    STALE_CREATED = "STALE_CREATED"


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    """A stored run and why this delivery resolved to it."""

    kind: AcceptanceKind
    run: VerificationRun


class RunNotFoundError(LookupError):
    """Raised when a requested verification run does not exist."""


class ConcurrentRunUpdateError(RuntimeError):
    """Raised when optimistic state versioning detects a stale writer."""


class VerificationRunStore(Protocol):
    """Persistence contract used by the control plane."""

    def accept_pull_request(self, event: PullRequestEvent) -> AcceptanceResult: ...

    def get_run(self, run_id: UUID) -> VerificationRun: ...

    def list_runs(self, *, repository: str, pr_number: int) -> list[VerificationRun]: ...

    def transition_run(
        self, *, run_id: UUID, expected_version: int, transition: RunTransition
    ) -> VerificationRun: ...


class SqliteVerificationRunStore:
    """Transactional local store with delivery/revision idempotency."""

    def __init__(
        self,
        database_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database_path = database_path.resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._initialize()

    def accept_pull_request(self, event: PullRequestEvent) -> AcceptanceResult:
        """Atomically deduplicate, supersede stale work, and persist one delivery."""
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")

            duplicate_delivery = connection.execute(
                """
                SELECT runs.*
                FROM deliveries
                JOIN runs ON runs.run_id = deliveries.run_id
                WHERE deliveries.delivery_id = ?
                """,
                (event.delivery_id,),
            ).fetchone()
            if duplicate_delivery is not None:
                connection.commit()
                return AcceptanceResult(
                    kind=AcceptanceKind.DUPLICATE_DELIVERY,
                    run=self._row_to_run(duplicate_delivery),
                )

            current_row = connection.execute(
                """
                SELECT * FROM runs
                WHERE repository = ? AND pr_number = ? AND revision_state = ?
                """,
                (event.repository, event.pr_number, RevisionState.CURRENT),
            ).fetchone()
            current_run = self._row_to_run(current_row) if current_row is not None else None

            duplicate_revision = connection.execute(
                """
                SELECT * FROM runs
                WHERE repository = ? AND pr_number = ? AND base_sha = ? AND head_sha = ?
                ORDER BY head_updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (event.repository, event.pr_number, event.base_sha, event.head_sha),
            ).fetchone()
            if duplicate_revision is not None:
                run = self._row_to_run(duplicate_revision)
                duplicate_is_current = current_run is not None and run.run_id == current_run.run_id
                duplicate_is_not_newer = (
                    current_run is not None and event.head_updated_at <= current_run.head_updated_at
                )
                if current_run is None or duplicate_is_current or duplicate_is_not_newer:
                    if event.head_updated_at > run.head_updated_at:
                        connection.execute(
                            """
                            UPDATE runs
                            SET head_updated_at = ?, event_action = ?, updated_at = ?,
                                version = version + 1
                            WHERE run_id = ?
                            """,
                            (
                                event.head_updated_at.isoformat(),
                                event.action,
                                now.isoformat(),
                                str(run.run_id),
                            ),
                        )
                        run = self._require_run(connection, run.run_id)
                    self._insert_delivery(connection, event.delivery_id, run.run_id, now)
                    connection.commit()
                    return AcceptanceResult(kind=AcceptanceKind.DUPLICATE_REVISION, run=run)

            run_id = uuid4()
            is_stale = (
                current_run is not None and event.head_updated_at <= current_run.head_updated_at
            )

            if current_run is not None and not is_stale:
                connection.execute(
                    """
                    UPDATE runs
                    SET revision_state = ?, superseded_by_run_id = ?,
                        lifecycle = CASE WHEN lifecycle = ? THEN lifecycle ELSE ? END,
                        terminal_reason = CASE WHEN lifecycle = ? THEN terminal_reason ELSE ? END,
                        updated_at = ?, version = version + 1
                    WHERE run_id = ?
                    """,
                    (
                        RevisionState.SUPERSEDED,
                        str(run_id),
                        RunLifecycle.TERMINAL,
                        RunLifecycle.TERMINAL,
                        RunLifecycle.TERMINAL,
                        TerminalReason.SUPERSEDED,
                        now.isoformat(),
                        str(current_run.run_id),
                    ),
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
                lifecycle=RunLifecycle.TERMINAL if is_stale else RunLifecycle.ACCEPTED,
                phase=RunPhase.CONTEXT,
                terminal_reason=TerminalReason.SUPERSEDED if is_stale else None,
                publication_state=PublicationState.NOT_STARTED,
                revision_state=RevisionState.SUPERSEDED if is_stale else RevisionState.CURRENT,
                superseded_by_run_id=current_run.run_id if is_stale and current_run else None,
                created_at=now,
                updated_at=now,
                version=1,
            )
            self._insert_run(connection, run)
            self._insert_delivery(connection, event.delivery_id, run.run_id, now)
            connection.commit()
            return AcceptanceResult(
                kind=AcceptanceKind.STALE_CREATED if is_stale else AcceptanceKind.CREATED,
                run=run,
            )

    def get_run(self, run_id: UUID) -> VerificationRun:
        """Load one durable run by identity."""
        with closing(self._connect()) as connection:
            return self._require_run(connection, run_id)

    def list_runs(self, *, repository: str, pr_number: int) -> list[VerificationRun]:
        """Return the auditable revision history for one pull request."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs
                WHERE repository = ? AND pr_number = ?
                ORDER BY created_at, run_id
                """,
                (repository.lower(), pr_number),
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def transition_run(
        self, *, run_id: UUID, expected_version: int, transition: RunTransition
    ) -> VerificationRun:
        """Apply one validated optimistic transition without touching identity fields."""
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_run(connection, run_id)
            if current.version != expected_version:
                raise ConcurrentRunUpdateError(
                    f"run version is {current.version}, expected {expected_version}"
                )
            updated = apply_run_transition(current, transition, updated_at=now)
            values = self._run_values(updated)
            assignments = ", ".join(f"{column} = ?" for column in values if column != "run_id")
            parameters = [value for column, value in values.items() if column != "run_id"]
            cursor = connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ? AND version = ?",
                (*parameters, str(run_id), expected_version),
            )
            if cursor.rowcount != 1:
                raise ConcurrentRunUpdateError("run changed during transition")
            connection.commit()
            return updated

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL,
                    event_action TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    head_updated_at TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    terminal_reason TEXT,
                    mechanical_evidence_status TEXT,
                    claim_outcome TEXT,
                    publication_state TEXT NOT NULL,
                    revision_state TEXT NOT NULL,
                    superseded_by_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    received_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_current_run_per_pull_request
                    ON runs(repository, pr_number)
                    WHERE revision_state = 'CURRENT';

                CREATE INDEX IF NOT EXISTS runs_by_pull_request
                    ON runs(repository, pr_number, created_at);

                CREATE INDEX IF NOT EXISTS runs_by_revision
                    ON runs(repository, pr_number, base_sha, head_sha, head_updated_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _insert_delivery(
        connection: sqlite3.Connection,
        delivery_id: str,
        run_id: UUID,
        received_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO deliveries(delivery_id, run_id, received_at) VALUES (?, ?, ?)",
            (delivery_id, str(run_id), received_at.isoformat()),
        )

    def _insert_run(self, connection: sqlite3.Connection, run: VerificationRun) -> None:
        values = self._run_values(run)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        connection.execute(
            f"INSERT INTO runs ({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )

    @staticmethod
    def _run_values(run: VerificationRun) -> dict[str, str | int | None]:
        return {
            "run_id": str(run.run_id),
            "delivery_id": run.delivery_id,
            "event_action": run.event_action,
            "repository": run.repository,
            "pr_number": run.pr_number,
            "base_sha": run.base_sha,
            "head_sha": run.head_sha,
            "head_updated_at": run.head_updated_at.isoformat(),
            "lifecycle": run.lifecycle,
            "phase": run.phase,
            "terminal_reason": run.terminal_reason,
            "mechanical_evidence_status": run.mechanical_evidence_status,
            "claim_outcome": run.claim_outcome,
            "publication_state": run.publication_state,
            "revision_state": run.revision_state,
            "superseded_by_run_id": (
                str(run.superseded_by_run_id) if run.superseded_by_run_id else None
            ),
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "version": run.version,
        }

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> VerificationRun:
        return VerificationRun.model_validate(dict(row))

    def _require_run(self, connection: sqlite3.Connection, run_id: UUID) -> VerificationRun:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (str(run_id),)).fetchone()
        if row is None:
            raise RunNotFoundError(f"verification run not found: {run_id}")
        return self._row_to_run(row)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("store clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)
