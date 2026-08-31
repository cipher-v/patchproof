"""One explicitly authorized live ADK + Vertex Claim Investigator smoke."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from patchproof.adk_claim_agent import DEFAULT_CLAIM_MODEL
from patchproof.adk_claim_investigator import AdkGeminiClaimInvestigator
from patchproof.claim_agent import ClaimSelectionDisposition, PullRequestNarrative
from patchproof.claim_investigation import StartingContextBudget
from patchproof.claim_investigator import (
    GitClaimInvestigatorFactory,
    InvestigatorDecision,
    InvestigatorTurnRequest,
    RawInvestigatorResponse,
)
from patchproof.gemini_provider import (
    GeminiProviderConfig,
    GeminiProviderSurface,
    preflight_vertex_authentication,
)

_BASE_SOURCE = """__all__ = ["Console"]


class Console:
    def render(self, text, style):
        return style + text
"""

_HEAD_SOURCE = """__all__ = ["Console"]


class Console:
    def render(self, text, style):
        return terminate_lines(style + text)


def terminate_lines(value):
    return value.replace("\\n", "|\\n")
"""


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _commit_pair(repository: Path) -> tuple[str, str]:
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "patchproof@example.invalid")
    _git(repository, "config", "user.name", "PatchProof Live Smoke")
    package = repository / "pkg"
    package.mkdir()
    source = package / "console.py"
    source.write_text(_BASE_SOURCE, encoding="utf-8")
    _git(repository, "add", "pkg/console.py")
    _git(repository, "commit", "-m", "base console behavior")
    base_sha = _git(repository, "rev-parse", "HEAD")
    source.write_text(_HEAD_SOURCE, encoding="utf-8")
    _git(repository, "add", "pkg/console.py")
    _git(repository, "commit", "-m", "change console behavior")
    return base_sha, _git(repository, "rev-parse", "HEAD")


class _RecordingModel:
    """Delegate every turn to the real adapter while retaining safe accounting."""

    def __init__(self, delegate: AdkGeminiClaimInvestigator) -> None:
        self.delegate = delegate
        self.turns: list[dict[str, object]] = []

    async def invoke(self, request: InvestigatorTurnRequest) -> RawInvestigatorResponse:
        response = await self.delegate.invoke(request)
        decision = InvestigatorDecision.model_validate_json(response.text)
        self.turns.append(
            {
                "action": decision.action.value,
                "requested_tools": [
                    {"tool": item.tool, "arguments": item.to_arguments()}
                    for item in decision.tool_calls
                ],
                "usage": response.usage.model_dump(mode="json"),
            }
        )
        return response


def test_live_vertex_claim_investigator_uses_repository_tool_then_concludes(
    tmp_path: Path,
) -> None:
    if os.environ.get("PATCHPROOF_RUN_LIVE_INVESTIGATOR") != "1":
        pytest.skip("set PATCHPROOF_RUN_LIVE_INVESTIGATOR=1 for the authorized live smoke")

    provider = GeminiProviderConfig.from_environment()
    assert provider.provider_surface is GeminiProviderSurface.VERTEX_AI
    auth = preflight_vertex_authentication(provider)
    assert auth.credentials_available is True

    repository = tmp_path / "claim-investigator-live"
    base_sha, head_sha = _commit_pair(repository)
    model_name = os.environ.get("PATCHPROOF_GEMINI_MODEL", DEFAULT_CLAIM_MODEL)
    recording_model = _RecordingModel(
        AdkGeminiClaimInvestigator(model_name=model_name, provider_config=provider)
    )
    investigator = GitClaimInvestigatorFactory(
        model=recording_model,
        source_repository=repository,
        # Deliberately small deterministic pre-fetch: the narrative and context identify
        # the changed area but omit the source fact needed for a differential claim.
        context_budget=StartingContextBudget(max_shared_observables=1),
    ).build(base_sha=base_sha, head_sha=head_sha)

    result = asyncio.run(
        investigator.investigate(
            narrative=PullRequestNarrative.from_untrusted(
                title="Update console behavior",
                body="Behavioral details are intentionally omitted from this narrative.",
            ),
            diff="",
        )
    )

    selection = result.agent_result.selection
    claim = selection.claim
    report = {
        "base_sha": base_sha,
        "head_sha": head_sha,
        "provider": provider.provider_surface.value,
        "project": provider.project,
        "location": provider.location,
        "model": model_name,
        "model_turns": recording_model.turns,
        "transcript_turns": result.transcript.turns,
        "tool_calls": [item.model_dump(mode="json") for item in result.transcript.tool_calls],
        "disposition": selection.disposition.value,
        "selected_interface": claim.shared_interface if claim is not None else None,
        "claim_summary": claim.summary if claim is not None else None,
        "abstention_explanation": selection.explanation if claim is None else None,
    }
    print("PATCHPROOF_LIVE_INVESTIGATOR_REPORT=" + json.dumps(report, sort_keys=True))

    assert selection.disposition in set(ClaimSelectionDisposition)
    assert result.transcript.turns == len(recording_model.turns)
