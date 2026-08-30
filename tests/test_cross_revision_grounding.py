"""A differential experiment can only run on an interface both revisions have.

Regression coverage for `docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md` bottleneck 3.
Signature grounding and every source snippet were previously derived from HEAD alone,
so a pull request that adds a helper presented that helper to the agent with no way to
ask whether BASE has it. Rich #3938 in the sealed unseen holdout tested
``Segment.split_lines_terminator``, which BASE does not define, so the test could only
show that a new method exists.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from patchproof.context_retrieval import (
    CrossRevisionInterfaces,
    DeterministicContextRetriever,
    SnippetKind,
)
from patchproof.execution_contract import ExecutionContract
from patchproof.test_generation import (
    CandidateIssueCode,
    CandidateTestProposal,
    CandidateTestValidator,
    CandidateValidationError,
)

_CONTRACT = ExecutionContract.model_validate(
    {
        "version": 1,
        "python": "3.12",
        "install": [["uv", "sync", "--frozen"]],
        "test": {"command": ["python", "-m", "pytest"]},
        "allowed_test_paths": ["tests/patchproof_generated/"],
        "timeout_seconds": 30,
    }
)

_BASE_RENDERER = """from helpers import normalize_style


class Renderer:
    def render(self, text: str, style: str) -> str:
        return normalize_style(style) + text
"""

# HEAD introduces a new helper method, exactly the shape that trapped the agent on Rich.
_HEAD_RENDERER = """from helpers import normalize_style


class Renderer:
    def render(self, text: str, style: str) -> str:
        return self.split_terminated(normalize_style(style) + text)

    def split_terminated(self, value: str) -> str:
        return value.replace("\\n", "|\\n")
"""

_HELPERS = '''def normalize_style(style: str) -> str:
    """Collapse an empty style to a neutral marker instead of returning it verbatim."""
    if not style.strip():
        return "<none>"
    return style.lower()
'''


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return completed.stdout.strip()


@pytest.fixture
def helper_introducing_repository() -> Iterator[tuple[Path, str, str]]:
    """A pull request that adds a helper method and delegates to an unchanged helper."""
    root = Path.cwd() / ".test-runs" / f"interfaces-{uuid.uuid4().hex}"
    repository = root / "repo"
    repository.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.name", "PatchProof Tests")
    _git(repository, "config", "user.email", "patchproof-tests@example.invalid")
    _git(repository, "config", "commit.gpgsign", "false")

    (repository / "helpers.py").write_text(_HELPERS, encoding="utf-8")
    (repository / "renderer.py").write_text(_BASE_RENDERER, encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_renderer.py").write_text(
        "from renderer import Renderer\n\n\ndef test_render():\n"
        "    assert Renderer().render('a', 'BOLD') == 'bolda'\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial renderer")
    base_sha = _git(repository, "rev-parse", "HEAD")

    (repository / "renderer.py").write_text(_HEAD_RENDERER, encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "terminate rendered lines")
    head_sha = _git(repository, "rev-parse", "HEAD")

    try:
        yield repository, base_sha, head_sha
    finally:
        import shutil
        import stat

        def _force(function, target: str, _error) -> None:
            try:
                Path(target).chmod(stat.S_IWRITE)
                function(target)
            except FileNotFoundError:
                return

        shutil.rmtree(root, onexc=_force)


def test_partition_separates_shared_from_head_only_symbols(
    helper_introducing_repository: tuple[Path, str, str],
) -> None:
    repository, base_sha, head_sha = helper_introducing_repository
    context = DeterministicContextRetriever(source_repository=repository).retrieve(
        base_sha=base_sha, head_sha=head_sha
    )

    interfaces = context.interfaces
    assert "Renderer.split_terminated" in interfaces.new_on_head
    assert "Renderer.render" in interfaces.present_on_both
    assert "Renderer" in interfaces.present_on_both
    assert "split_terminated" in interfaces.head_only_leaf_names
    assert "render" not in interfaces.head_only_leaf_names


def test_unchanged_called_helper_body_is_retrieved_in_full(
    helper_introducing_repository: tuple[Path, str, str],
) -> None:
    """The meaning of the change lives in the helper it delegates to."""
    repository, base_sha, head_sha = helper_introducing_repository
    context = DeterministicContextRetriever(source_repository=repository).retrieve(
        base_sha=base_sha, head_sha=head_sha
    )

    helpers = [snippet for snippet in context.snippets if snippet.kind is SnippetKind.CALLED_HELPER]
    assert helpers, "the directly called unchanged helper must be retrieved"
    body = "\n".join(snippet.content for snippet in helpers)
    # Not merely the name: the branch that decides the behavior.
    assert "def normalize_style" in body
    assert "<none>" in body


def test_excluded_paths_never_reach_called_helper_snippets(
    helper_introducing_repository: tuple[Path, str, str],
) -> None:
    """Holdout exclusions must hold for the new snippet kind too."""
    repository, base_sha, head_sha = helper_introducing_repository
    context = DeterministicContextRetriever(
        source_repository=repository,
        excluded_paths=frozenset({"helpers.py"}),
    ).retrieve(base_sha=base_sha, head_sha=head_sha)

    for snippet in context.snippets:
        assert snippet.path != "helpers.py"


def _proposal(source: str) -> CandidateTestProposal:
    return CandidateTestProposal(
        candidate_id="candidate-initial",
        target_path="tests/patchproof_generated/test_patchproof_generated_initial.py",
        test_function="test_patchproof_generated_behavior",
        source=source,
        rationale="checks the claimed behavior",
    )


def _validate(source: str, interfaces: CrossRevisionInterfaces, context):
    return CandidateTestValidator().validate(
        proposal=_proposal(source),
        context=context.model_copy(update={"interfaces": interfaces}),
        contract=_CONTRACT,
        existing_paths=frozenset(),
    )


def test_candidate_calling_a_head_only_method_is_rejected(
    helper_introducing_repository: tuple[Path, str, str],
) -> None:
    """The Rich failure shape, caught mechanically and without naming any project."""
    repository, base_sha, head_sha = helper_introducing_repository
    context = DeterministicContextRetriever(source_repository=repository).retrieve(
        base_sha=base_sha, head_sha=head_sha
    )
    source = (
        "from renderer import Renderer\n\n\ndef test_patchproof_generated_behavior():\n"
        "    assert Renderer().split_terminated('a\\nb') == 'a|\\nb'\n"
    )

    with pytest.raises(CandidateValidationError) as rejected:
        _validate(source, context.interfaces, context)

    assert rejected.value.issues[0].code is CandidateIssueCode.HEAD_ONLY_INTERFACE
    assert "split_terminated" in rejected.value.issues[0].message


def test_candidate_using_the_shared_interface_is_accepted(
    helper_introducing_repository: tuple[Path, str, str],
) -> None:
    repository, base_sha, head_sha = helper_introducing_repository
    context = DeterministicContextRetriever(source_repository=repository).retrieve(
        base_sha=base_sha, head_sha=head_sha
    )
    source = (
        "from renderer import Renderer\n\n\ndef test_patchproof_generated_behavior():\n"
        "    assert Renderer().render('a\\nb', 'BOLD') == 'bolda|\\nb'\n"
    )

    validated = _validate(source, context.interfaces, context)

    assert validated.behavior_fingerprint


def test_no_head_only_symbols_means_no_extra_restriction() -> None:
    """The check must be inert when a pull request adds nothing new."""
    assert CrossRevisionInterfaces().head_only_leaf_names == frozenset()


def test_claim_must_name_an_interface_present_on_both_revisions(
    helper_introducing_repository: tuple[Path, str, str],
) -> None:
    """A claim aimed at a HEAD-only symbol is rejected at selection, not three attempts later."""
    import asyncio

    from patchproof.claim_agent import (
        AffectedSymbolRef,
        BehavioralClaimAgent,
        BehavioralClaimDraft,
        ClaimSelectionDisposition,
        ClaimSelectionDraft,
        InvalidClaimAgentOutput,
        ModelUsage,
        PullRequestNarrative,
        RawClaimModelResponse,
        SupportingContextRef,
    )

    repository, base_sha, head_sha = helper_introducing_repository
    context = DeterministicContextRetriever(source_repository=repository).retrieve(
        base_sha=base_sha, head_sha=head_sha
    )
    symbol = next(
        item for item in context.changed_symbols if item.qualified_name.startswith("Renderer")
    )
    snippet = next(item for item in context.snippets if item.path == symbol.path)

    def _draft(shared_interface: str) -> str:
        return ClaimSelectionDraft(
            disposition=ClaimSelectionDisposition.SELECTED,
            claim=BehavioralClaimDraft(
                summary="Rendered lines are terminated.",
                observable_operation="Renderer.render(text, style)",
                trigger_condition="The text contains a newline.",
                expected_head_observation="The newline is preceded by a terminator marker.",
                expected_base_hypothesis="The newline is emitted without a terminator marker.",
                shared_interface=shared_interface,
                preconditions=("A renderer instance exists.",),
                action="Render text containing a newline.",
                expected_behavior="The output marks the line terminator.",
                affected_symbols=(
                    AffectedSymbolRef(path=symbol.path, qualified_name=symbol.qualified_name),
                ),
                supporting_context=(
                    SupportingContextRef(
                        path=snippet.path,
                        start_line=snippet.start_line,
                        end_line=snippet.end_line,
                        relevance="The changed render body delegates to the new helper.",
                    ),
                ),
                confidence=0.9,
            ),
            explanation="The rendered output gains a line terminator marker.",
        ).model_dump_json()

    class FixedModel:
        def __init__(self, payload: str) -> None:
            self.payload = payload

        async def invoke(self, request):
            del request
            return RawClaimModelResponse(
                text=self.payload,
                usage=ModelUsage(model_name="test-model", duration_seconds=0.1),
            )

    narrative = PullRequestNarrative.from_untrusted(title="Terminate rendered lines")

    with pytest.raises(InvalidClaimAgentOutput, match="only on HEAD"):
        asyncio.run(
            BehavioralClaimAgent(model=FixedModel(_draft("split_terminated"))).select_claim(
                context=context, narrative=narrative
            )
        )

    result = asyncio.run(
        BehavioralClaimAgent(model=FixedModel(_draft("Renderer.render"))).select_claim(
            context=context, narrative=narrative
        )
    )
    assert result.selection.claim is not None
    assert result.selection.claim.expected_base_hypothesis

    # Regression: an empty shared partition must not disable the HEAD-only check.
    head_only_context = context.model_copy(
        update={
            "interfaces": CrossRevisionInterfaces(
                present_on_both=(),
                new_on_head=("Renderer.split_terminated",),
            )
        }
    )
    with pytest.raises(InvalidClaimAgentOutput, match="only on HEAD"):
        asyncio.run(
            BehavioralClaimAgent(model=FixedModel(_draft("split_terminated"))).select_claim(
                context=head_only_context,
                narrative=narrative,
            )
        )
