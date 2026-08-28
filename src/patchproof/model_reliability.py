"""Bounded transient retry policy shared by PatchProof semantic task adapters."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Protocol, TypeVar

import httpx
from google.genai import errors as genai_errors

from patchproof.models import ChallengeResult

RequestT = TypeVar("RequestT")
ResponseT = TypeVar("ResponseT")


class ModelInvocationFailure(RuntimeError):
    """Normalized provider failure carrying only a deterministic retry property."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def is_transient_provider_error(error: BaseException) -> bool:
    """Classify only explicit transport, timeout, throttling, and server failures as transient."""
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(
            current,
            (
                TimeoutError,
                asyncio.TimeoutError,
                httpx.TimeoutException,
                httpx.NetworkError,
                genai_errors.ServerError,
            ),
        ):
            return True
        if isinstance(current, genai_errors.ClientError) and current.code in {
            408,
            409,
            425,
            429,
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


def is_transient_event_code(value: str | None) -> bool:
    """Recognize bounded provider status names without inspecting provider prose."""
    normalized = (value or "").strip().upper()
    return normalized in {
        "408",
        "409",
        "425",
        "429",
        "500",
        "502",
        "503",
        "504",
        "DEADLINE_EXCEEDED",
        "INTERNAL",
        "RESOURCE_EXHAUSTED",
        "SERVICE_UNAVAILABLE",
        "UNAVAILABLE",
    }


class StructuredModel(Protocol[RequestT, ResponseT]):
    async def invoke(self, request: RequestT) -> ResponseT: ...


class BoundedRetryingModel[RequestT, ResponseT]:
    """Retry one logical semantic call at most once, and only for explicit transient failures."""

    def __init__(self, model: StructuredModel[RequestT, ResponseT]) -> None:
        self.model = model

    async def invoke(self, request: RequestT) -> ResponseT:
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await self.model.invoke(request)
            except ModelInvocationFailure as error:
                if not error.retryable or attempts >= 2:
                    raise
            else:
                usage = response.usage.model_copy(update={"provider_attempts": attempts})
                return replace(response, usage=usage)


class EvidenceAssessor(Protocol[ResponseT]):
    async def assess(
        self, *, claim, candidate_source: str, challenge: ChallengeResult
    ) -> ResponseT: ...


class BoundedRetryingEvidenceAssessor[ResponseT]:
    """Apply the same one-retry transient policy to the final semantic assessment task."""

    def __init__(self, assessor: EvidenceAssessor[ResponseT]) -> None:
        self.assessor = assessor

    async def assess(
        self, *, claim, candidate_source: str, challenge: ChallengeResult
    ) -> ResponseT:
        attempts = 0
        while True:
            attempts += 1
            try:
                response = await self.assessor.assess(
                    claim=claim,
                    candidate_source=candidate_source,
                    challenge=challenge,
                )
            except ModelInvocationFailure as error:
                if not error.retryable or attempts >= 2:
                    raise
            else:
                usage = response.usage.model_copy(update={"provider_attempts": attempts})
                return response.model_copy(update={"usage": usage})
