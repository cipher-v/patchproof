"""Tests for bounded, explicitly classified semantic-provider retries."""

from __future__ import annotations

import asyncio

import pytest
from google.genai.errors import ClientError, ServerError

from patchproof.claim_agent import ModelUsage, RawClaimModelResponse
from patchproof.model_reliability import (
    BoundedRetryingModel,
    ModelInvocationFailure,
    is_transient_event_code,
    is_transient_provider_error,
)


def _response() -> RawClaimModelResponse:
    return RawClaimModelResponse(
        text='{"result":"ok"}',
        usage=ModelUsage(model_name="fake-gemini", duration_seconds=0.01),
    )


class SequencedModel:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = 0

    async def invoke(self, request):
        del request
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def test_one_transient_failure_gets_exactly_one_retry_and_is_audited() -> None:
    model = SequencedModel(
        ModelInvocationFailure("temporary", retryable=True),
        _response(),
    )

    response = asyncio.run(BoundedRetryingModel(model).invoke(object()))

    assert model.calls == 2
    assert response.usage.provider_attempts == 2


def test_permanent_failure_is_not_retried() -> None:
    model = SequencedModel(ModelInvocationFailure("permanent", retryable=False))

    with pytest.raises(ModelInvocationFailure, match="permanent"):
        asyncio.run(BoundedRetryingModel(model).invoke(object()))
    assert model.calls == 1


def test_two_transient_failures_exhaust_budget_without_a_third_call() -> None:
    model = SequencedModel(
        ModelInvocationFailure("temporary-1", retryable=True),
        ModelInvocationFailure("temporary-2", retryable=True),
    )

    with pytest.raises(ModelInvocationFailure, match="temporary-2"):
        asyncio.run(BoundedRetryingModel(model).invoke(object()))
    assert model.calls == 2


def test_only_known_provider_statuses_are_transient() -> None:
    assert is_transient_provider_error(ServerError(503, {"message": "unavailable"}))
    assert is_transient_provider_error(ClientError(429, {"message": "throttled"}))
    assert not is_transient_provider_error(ClientError(400, {"message": "bad request"}))
    assert not is_transient_provider_error(RuntimeError("unknown"))


def test_named_and_numeric_transient_event_codes_share_the_bounded_policy() -> None:
    assert is_transient_event_code("RESOURCE_EXHAUSTED")
    assert is_transient_event_code("429")
    assert is_transient_event_code("503")
    assert not is_transient_event_code("PERMISSION_DENIED")
    assert not is_transient_event_code("403")
