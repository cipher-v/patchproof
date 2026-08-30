"""Persistent workflow-store abstraction and local SQLite implementation."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from patchproof.claim_agent import ModelUsage
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


class StoredEvidenceConflictError(RuntimeError):
    """Raised if a caller tries to replace immutable evidence for one run."""


@dataclass(frozen=True, slots=True)
class StoredEvidence:
    """Content-addressed serialized evidence retained independently of workflow state."""

    run_id: UUID
    document_json: str
    sha256: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CheckPublication:
    """Retry metadata for one run's single logical GitHub Check."""

    run_id: UUID
    payload_sha256: str
    check_run_id: int | None
    attempt_count: int
    last_error: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RunFailureRecord:
    """Sanitized terminal worker failure separated from evidence and provider details."""

    run_id: UUID
    phase: RunPhase
    error_code: str
    summary: str
    retryable: bool
    created_at: datetime
    model_usage: ModelUsage | None = None
    raw_response_sha256: str | None = None


class VerificationRunStore(Protocol):
    """Persistence contract used by the control plane."""

    def accept_pull_request(self, event: PullRequestEvent) -> AcceptanceResult: ...

    def get_run(self, run_id: UUID) -> VerificationRun: ...

    def list_runs(self, *, repository: str, pr_number: int) -> list[VerificationRun]: ...

    def list_recent_runs(self, *, limit: int) -> list[VerificationRun]: ...

    def transition_run(
        self, *, run_id: UUID, expected_version: int, transition: RunTransition
    ) -> VerificationRun: ...

    def save_evidence(self, *, run_id: UUID, document_json: str, sha256: str) -> StoredEvidence: ...

    def get_evidence(self, run_id: UUID) -> StoredEvidence | None: ...

    def begin_publication(self, *, run_id: UUID, payload_sha256: str) -> CheckPublication: ...

    def get_publication(self, run_id: UUID) -> CheckPublication | None: ...

    def set_check_run_id(self, *, run_id: UUID, check_run_id: int) -> CheckPublication: ...

    def record_publication_error(self, *, run_id: UUID, error: str) -> CheckPublication: ...

    def fail_run(
        self,
        *,
        run_id: UUID,
        error_code: str,
        summary: str,
        retryable: bool,
        model_usage: ModelUsage | None = None,
        raw_response_sha256: str | None = None,
    ) -> RunFailureRecord: ...

    def get_failure(self, run_id: UUID) -> RunFailureRecord | None: ...


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
                            SET head_updated_at = ?, event_action = ?, title = ?, body = ?,
                                installation_id = ?, updated_at = ?,
                                version = version + 1
                            WHERE run_id = ?
                            """,
                            (
                                event.head_updated_at.isoformat(),
                                event.action,
                                event.title,
                                event.body,
                                event.installation_id,
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
                title=event.title,
                body=event.body,
                installation_id=event.installation_id,
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

    def list_recent_runs(self, *, limit: int) -> list[VerificationRun]:
        """Return a bounded newest-first projection for product discovery."""
        if not 1 <= limit <= 8:
            raise ValueError("recent run limit must be between one and eight")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (limit,),
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

    def save_evidence(self, *, run_id: UUID, document_json: str, sha256: str) -> StoredEvidence:
        """Insert one immutable evidence document, or return an identical prior insert."""
        if hashlib.sha256(document_json.encode("utf-8")).hexdigest() != sha256:
            raise ValueError("evidence SHA-256 does not match serialized evidence")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            row = connection.execute(
                "SELECT * FROM evidence_records WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO evidence_records(run_id, document_json, sha256, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(run_id), document_json, sha256, now.isoformat()),
                )
                row = connection.execute(
                    "SELECT * FROM evidence_records WHERE run_id = ?", (str(run_id),)
                ).fetchone()
            elif row["sha256"] != sha256 or row["document_json"] != document_json:
                raise StoredEvidenceConflictError("stored evidence cannot be overwritten")
            connection.commit()
            return self._row_to_evidence(row)

    def get_evidence(self, run_id: UUID) -> StoredEvidence | None:
        """Load the immutable evidence document for a run, if computation completed."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM evidence_records WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return self._row_to_evidence(row) if row is not None else None

    def begin_publication(self, *, run_id: UUID, payload_sha256: str) -> CheckPublication:
        """Start or retry publication without changing immutable evidence or its payload."""
        if re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None:
            raise ValueError("publication payload hash must be a lowercase SHA-256 value")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            row = connection.execute(
                "SELECT * FROM check_publications WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO check_publications(
                        run_id, payload_sha256, check_run_id, attempt_count, last_error, updated_at
                    ) VALUES (?, ?, NULL, 1, NULL, ?)
                    """,
                    (str(run_id), payload_sha256, now.isoformat()),
                )
            else:
                if row["payload_sha256"] != payload_sha256:
                    raise StoredEvidenceConflictError("publication payload cannot be replaced")
                connection.execute(
                    """
                    UPDATE check_publications
                    SET attempt_count = attempt_count + 1, last_error = NULL, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (now.isoformat(), str(run_id)),
                )
            row = connection.execute(
                "SELECT * FROM check_publications WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            connection.commit()
            return self._row_to_publication(row)

    def get_publication(self, run_id: UUID) -> CheckPublication | None:
        """Load publication metadata without incrementing the attempt counter."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM check_publications WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return self._row_to_publication(row) if row is not None else None

    def set_check_run_id(self, *, run_id: UUID, check_run_id: int) -> CheckPublication:
        """Remember the remote Check identity; assigning a different ID is forbidden."""
        if check_run_id <= 0:
            raise ValueError("GitHub Check run ID must be positive")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM check_publications WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise RunNotFoundError("publication attempt has not started")
            prior = row["check_run_id"]
            if prior is not None and prior != check_run_id:
                raise StoredEvidenceConflictError("GitHub Check identity cannot be replaced")
            connection.execute(
                "UPDATE check_publications SET check_run_id = ?, updated_at = ? WHERE run_id = ?",
                (check_run_id, now.isoformat(), str(run_id)),
            )
            row = connection.execute(
                "SELECT * FROM check_publications WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            connection.commit()
            return self._row_to_publication(row)

    def record_publication_error(self, *, run_id: UUID, error: str) -> CheckPublication:
        """Persist a bounded non-secret failure summary for operational retry."""
        now = self._now()
        bounded = error.strip()[:1_000] or "GitHub Check publication failed"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE check_publications SET last_error = ?, updated_at = ? WHERE run_id = ?
                """,
                (bounded, now.isoformat(), str(run_id)),
            )
            if cursor.rowcount != 1:
                raise RunNotFoundError("publication attempt has not started")
            row = connection.execute(
                "SELECT * FROM check_publications WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            connection.commit()
            return self._row_to_publication(row)

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
        """Atomically record one sanitized failure and terminate an active current run."""
        if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", error_code) is None:
            raise ValueError("worker failure code must be a bounded uppercase identifier")
        bounded_summary = summary.strip()[:500]
        if not bounded_summary:
            raise ValueError("worker failure summary must not be empty")
        if (
            raw_response_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", raw_response_sha256) is None
        ):
            raise ValueError("worker failure response hash must be lowercase SHA-256")
        now = self._now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._require_run(connection, run_id)
            prior = connection.execute(
                "SELECT * FROM run_failures WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if prior is not None:
                connection.commit()
                return self._row_to_failure(prior)
            updated = apply_run_transition(
                current,
                RunTransition(
                    lifecycle=RunLifecycle.TERMINAL,
                    terminal_reason=TerminalReason.FAILED,
                ),
                updated_at=now,
            )
            values = self._run_values(updated)
            assignments = ", ".join(f"{column} = ?" for column in values if column != "run_id")
            parameters = [value for column, value in values.items() if column != "run_id"]
            connection.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*parameters, str(run_id)),
            )
            connection.execute(
                """
                INSERT INTO run_failures(
                    run_id, phase, error_code, summary, retryable, created_at,
                    model_usage_json, raw_response_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    current.phase,
                    error_code,
                    bounded_summary,
                    int(retryable),
                    now.isoformat(),
                    model_usage.model_dump_json() if model_usage is not None else None,
                    raw_response_sha256,
                ),
            )
            row = connection.execute(
                "SELECT * FROM run_failures WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            connection.commit()
            return self._row_to_failure(row)

    def get_failure(self, run_id: UUID) -> RunFailureRecord | None:
        """Load one sanitized terminal worker failure, if present."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM run_failures WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return self._row_to_failure(row) if row is not None else None

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
                    title TEXT NOT NULL DEFAULT 'Untitled pull request',
                    body TEXT NOT NULL DEFAULT '',
                    installation_id INTEGER,
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

                CREATE TABLE IF NOT EXISTS evidence_records (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    document_json TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS check_publications (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    payload_sha256 TEXT NOT NULL,
                    check_run_id INTEGER,
                    attempt_count INTEGER NOT NULL,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_failures (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    phase TEXT NOT NULL,
                    error_code TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    retryable INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    model_usage_json TEXT,
                    raw_response_sha256 TEXT
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
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
            for name, definition in (
                ("title", "TEXT NOT NULL DEFAULT 'Untitled pull request'"),
                ("body", "TEXT NOT NULL DEFAULT ''"),
                ("installation_id", "INTEGER"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
            failure_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(run_failures)")
            }
            for name, definition in (
                ("model_usage_json", "TEXT"),
                ("raw_response_sha256", "TEXT"),
            ):
                if name not in failure_columns:
                    connection.execute(f"ALTER TABLE run_failures ADD COLUMN {name} {definition}")

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
            "title": run.title,
            "body": run.body,
            "installation_id": run.installation_id,
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

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> StoredEvidence:
        return StoredEvidence(
            run_id=UUID(row["run_id"]),
            document_json=row["document_json"],
            sha256=row["sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_publication(row: sqlite3.Row) -> CheckPublication:
        return CheckPublication(
            run_id=UUID(row["run_id"]),
            payload_sha256=row["payload_sha256"],
            check_run_id=row["check_run_id"],
            attempt_count=row["attempt_count"],
            last_error=row["last_error"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _row_to_failure(row: sqlite3.Row) -> RunFailureRecord:
        return RunFailureRecord(
            run_id=UUID(row["run_id"]),
            phase=RunPhase(row["phase"]),
            error_code=row["error_code"],
            summary=row["summary"],
            retryable=bool(row["retryable"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            model_usage=ModelUsage.model_validate_json(row["model_usage_json"])
            if row["model_usage_json"] is not None
            else None,
            raw_response_sha256=row["raw_response_sha256"],
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("store clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)
