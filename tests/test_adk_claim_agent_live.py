"""Credential-gated smoke test that makes one real ADK/Gemini structured-output call."""

from __future__ import annotations

import asyncio
import os

import pytest

from patchproof.adk_claim_agent import AdkGeminiClaimModel
from patchproof.claim_agent import BehavioralClaimAgent, PullRequestNarrative
from patchproof.context_retrieval import PullRequestContext, RetrievalStats


@pytest.mark.live_gemini
def test_live_gemini_can_return_a_valid_abstention_for_an_empty_diff() -> None:
    if os.environ.get("PATCHPROOF_RUN_LIVE_GEMINI") != "1":
        pytest.skip("set PATCHPROOF_RUN_LIVE_GEMINI=1 to allow one billable Gemini call")
    has_api_key = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    uses_vertex = (
        os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
        and bool(os.environ.get("GOOGLE_CLOUD_PROJECT"))
        and bool(os.environ.get("GOOGLE_CLOUD_LOCATION"))
    )
    if not has_api_key and not uses_vertex:
        pytest.skip("Gemini API or Vertex AI credentials are not configured")

    context = PullRequestContext(
        base_sha="a" * 40,
        head_sha="a" * 40,
        diff="",
        changed_files=(),
        changed_symbols=(),
        snippets=(),
        stats=RetrievalStats(
            changed_file_count=0,
            changed_python_file_count=0,
            python_path_count=0,
            test_files_scanned=0,
            reference_files_scanned=0,
            omitted_changed_files=0,
            truncated=False,
        ),
    )
    result = asyncio.run(
        BehavioralClaimAgent(model=AdkGeminiClaimModel()).select_claim(
            context=context,
            narrative=PullRequestNarrative.from_untrusted(
                title="Formatting-only change",
                body="There are no changed files between the immutable revisions.",
            ),
        )
    )

    assert result.selection.claim is None
    assert result.usage.model_name == "gemini-3.6-flash"
