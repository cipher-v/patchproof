"""Explicit, auditable per-task reasoning budgets for PatchProof's semantic tasks.

PatchProof runs one logical agent across three structured tasks. Each task has a
different reasoning difficulty, and the difficulty is not proportional to output
size: claim selection emits a few hundred tokens of JSON but must reverse-engineer
a behavioral trigger from a diff, while candidate generation emits far more text
for a comparatively mechanical transcription job.

Before this module the three ADK adapters each hard-coded `ThinkingLevel.LOW` and
a local output cap. The sealed unseen holdout
(`benchmarks/holdout/results/summary.json`) recorded 6,205 output tokens across 30
provider calls -- roughly 207 output tokens per call -- which is not enough budget
to attribute the observed failures to model capability rather than to
configuration. Centralizing the budgets makes the choice explicit, reviewable in
one place, and overridable per deployment without editing agent wiring.

These values affect only how much the model may think and emit. They cannot widen
what counts as evidence: `MechanicalEvidenceClassifier` and
`EvidenceWorkflow._validate_semantic_decision` remain the sole authorities on
whether a claim is supported.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from google.genai import types


class AgentTask(StrEnum):
    """One of the three structured semantic tasks the single agent performs."""

    CLAIM_SELECTION = "CLAIM_SELECTION"
    CANDIDATE_GENERATION = "CANDIDATE_GENERATION"
    EVIDENCE_ASSESSMENT = "EVIDENCE_ASSESSMENT"


@dataclass(frozen=True, slots=True)
class ReasoningBudget:
    """Bounded generation settings for one semantic task.

    `thinking_level` controls internal deliberation, `max_output_tokens` bounds the
    structured response, and `temperature` stays low so repeated runs over the same
    immutable context remain close to reproducible.
    """

    task: AgentTask
    thinking_level: types.ThinkingLevel
    max_output_tokens: int
    temperature: float = 0.1

    def __post_init__(self) -> None:
        if self.max_output_tokens <= 0:
            raise ValueError("reasoning budget output-token limit must be positive")
        if not 0.0 <= self.temperature <= 1.0:
            raise ValueError("reasoning budget temperature must be within [0.0, 1.0]")

    def generate_content_config(self) -> types.GenerateContentConfig:
        """Build the ADK generation config this budget describes."""
        return types.GenerateContentConfig(
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=self.thinking_level),
        )


# Claim selection is the hardest reasoning step in the pipeline: it must read a diff
# and commit to a falsifiable differential hypothesis. It gets MEDIUM thinking and a
# larger response budget because the claim schema now also carries an explicit BASE
# contrast hypothesis and a shared-interface commitment.
CLAIM_SELECTION_BUDGET = ReasoningBudget(
    task=AgentTask.CLAIM_SELECTION,
    thinking_level=types.ThinkingLevel.MEDIUM,
    max_output_tokens=4_096,
)

# Candidate generation transcribes an already-chosen hypothesis into pytest source.
# The initial candidate is close to mechanical, but a repair must diagnose bounded
# execution evidence, so this task also runs at MEDIUM.
CANDIDATE_GENERATION_BUDGET = ReasoningBudget(
    task=AgentTask.CANDIDATE_GENERATION,
    thinking_level=types.ThinkingLevel.MEDIUM,
    max_output_tokens=12_000,
)

# Evidence assessment answers one narrow relatedness question against facts that are
# already mechanically established. It stays at LOW deliberately: extra deliberation
# here cannot change what the mechanical layer permits, and a more talkative assessor
# is a false-positive risk rather than a benefit.
EVIDENCE_ASSESSMENT_BUDGET = ReasoningBudget(
    task=AgentTask.EVIDENCE_ASSESSMENT,
    thinking_level=types.ThinkingLevel.LOW,
    max_output_tokens=4_096,
)

DEFAULT_REASONING_BUDGETS: dict[AgentTask, ReasoningBudget] = {
    AgentTask.CLAIM_SELECTION: CLAIM_SELECTION_BUDGET,
    AgentTask.CANDIDATE_GENERATION: CANDIDATE_GENERATION_BUDGET,
    AgentTask.EVIDENCE_ASSESSMENT: EVIDENCE_ASSESSMENT_BUDGET,
}


def budget_for(task: AgentTask) -> ReasoningBudget:
    """Return the default reasoning budget for one semantic task."""
    return DEFAULT_REASONING_BUDGETS[task]
