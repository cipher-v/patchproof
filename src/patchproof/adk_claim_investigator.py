"""Google ADK/Gemini adapter for PatchProof's claim-investigation turn.

This is the production model boundary only. It converts one bounded
`InvestigatorTurnRequest` into one structured `InvestigatorDecision` and captures usage.
It deliberately owns none of the loop, none of the budgets, and none of the tool
execution: those live in `patchproof.claim_investigator` and
`patchproof.investigation_toolbox`, which keeps ADR-002's "deterministic services
enforce, the agent decides" boundary intact and keeps the whole orchestration testable
without a provider.

Why PatchProof drives the loop rather than ADK's function-calling runtime
------------------------------------------------------------------------

ADK can run a native tool loop, and that was the obvious alternative. It was not chosen:

* Budget enforcement would move inside the provider runtime, where PatchProof cannot
  guarantee the per-tool and total-call limits that the security contract depends on.
* Call accounting and replay would depend on provider-side transcripts rather than on
  PatchProof's own content-addressed record.
* Nothing in CI could exercise the orchestration, because every test would need a live
  provider.

The tool surface is small and the turn count is four, so an explicit loop costs little
and keeps all three properties. If a native tool loop is ever wanted, only this file and
the orchestrator's `invoke` call need to change; the toolbox and grounding are unaffected.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace
from uuid import uuid4

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from patchproof.adk_claim_agent import DEFAULT_CLAIM_MODEL
from patchproof.claim_agent import ModelUsage
from patchproof.claim_investigator import (
    CLAIM_INVESTIGATOR_INSTRUCTION,
    InvestigatorDecision,
    InvestigatorTurnRequest,
    RawInvestigatorResponse,
)
from patchproof.gemini_provider import (
    GeminiProviderConfig,
    NormalizedGeminiUsage,
    normalize_provider_event_failure,
    normalize_provider_failure,
)
from patchproof.model_reliability import ModelInvocationFailure
from patchproof.reasoning_budget import AgentTask, ReasoningBudget, budget_for

_MODEL_PATTERN = re.compile(r"gemini-(\d+)\.(\d+)-[a-z0-9.-]+")

#: Claim investigation is the hardest reasoning step in the pipeline and its output now
#: carries a full differential hypothesis plus an explicit interface identity, so it
#: reuses the claim-selection budget rather than declaring a looser one.
DEFAULT_INVESTIGATOR_MAX_OUTPUT_TOKENS = budget_for(AgentTask.CLAIM_SELECTION).max_output_tokens


class ClaimInvestigatorInvocationError(ModelInvocationFailure):
    """Raised when ADK cannot produce one final structured investigator decision."""


class AdkGeminiClaimInvestigator:
    """Invoke one stateless, tool-free ADK turn and capture its usage metadata.

    `tools=[]` is not an oversight. The model receives repository facts as *data* in the
    turn request and requests further facts as *structured output*; PatchProof executes
    those requests itself. The provider is never handed an executable capability.
    """

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_CLAIM_MODEL,
        provider_config: GeminiProviderConfig | None = None,
        max_output_tokens: int = DEFAULT_INVESTIGATOR_MAX_OUTPUT_TOKENS,
        timeout_seconds: float = 60.0,
        reasoning_budget: ReasoningBudget | None = None,
    ) -> None:
        match = _MODEL_PATTERN.fullmatch(model_name)
        if match is None or (int(match.group(1)), int(match.group(2))) < (3, 5):
            raise ValueError("investigator model must be an explicit Gemini 3.5-or-newer model")
        if max_output_tokens <= 0 or timeout_seconds <= 0:
            raise ValueError("ADK output-token and timeout budgets must be positive")
        self.reasoning_budget = reasoning_budget or budget_for(AgentTask.CLAIM_SELECTION)
        self.model_name = model_name
        self.provider_config = provider_config or GeminiProviderConfig.developer_api()
        self.adk_model = self.provider_config.adk_model(model_name)
        self.agent = LlmAgent(
            name="patchproof_agent",
            description="Investigates one pull request and selects a grounded claim or abstains.",
            model=self.adk_model,
            instruction=CLAIM_INVESTIGATOR_INSTRUCTION,
            input_schema=InvestigatorTurnRequest,
            output_schema=InvestigatorDecision,
            include_contents="none",
            tools=[],
            generate_content_config=replace(
                self.reasoning_budget, max_output_tokens=max_output_tokens
            ).generate_content_config(),
            timeout=timeout_seconds,
        )

    async def invoke(self, request: InvestigatorTurnRequest) -> RawInvestigatorResponse:
        """Run one isolated ADK session and retain only final JSON plus accounting facts."""
        session_id = f"investigator-{uuid4().hex}"
        user_id = "patchproof-control-plane"
        app_name = "patchproof"
        final_text: str | None = None
        model_version: str | None = None
        usage = NormalizedGeminiUsage()
        started = time.perf_counter()
        try:
            session_service = InMemorySessionService()
            await session_service.create_session(
                app_name=app_name, user_id=user_id, session_id=session_id
            )
            runner = Runner(agent=self.agent, app_name=app_name, session_service=session_service)
            message = types.Content(role="user", parts=[types.Part(text=request.model_dump_json())])
            async for event in runner.run_async(
                user_id=user_id, session_id=session_id, new_message=message
            ):
                if event.error_code or event.error_message:
                    failure = normalize_provider_event_failure(
                        self.provider_config, event.error_code, task="claim investigation"
                    )
                    raise ClaimInvestigatorInvocationError(
                        failure.message, retryable=failure.retryable
                    )
                model_version = event.model_version or model_version
                if event.usage_metadata is not None:
                    usage = usage.merge(event.usage_metadata)
                if event.is_final_response() and event.content and event.content.parts:
                    text_parts = [part.text for part in event.content.parts if part.text]
                    if text_parts:
                        final_text = "".join(text_parts)
        except ClaimInvestigatorInvocationError:
            raise
        except Exception as error:
            failure = normalize_provider_failure(
                self.provider_config, error, task="claim investigation"
            )
            raise ClaimInvestigatorInvocationError(
                failure.message, retryable=failure.retryable
            ) from error
        duration = time.perf_counter() - started
        if final_text is None:
            raise ClaimInvestigatorInvocationError("ADK claim investigation returned no final text")
        return RawInvestigatorResponse(
            text=final_text,
            usage=ModelUsage(
                model_name=self.model_name,
                model_version=model_version,
                prompt_tokens=usage.prompt_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=usage.total_tokens,
                cached_tokens=usage.cached_tokens,
                reasoning_tokens=usage.reasoning_tokens,
                duration_seconds=duration,
            ),
        )
