"""Tests for hard-mode provenance, deterministic fixtures, and honest metrics."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import patchproof.hard_mode as hard_mode_module
from patchproof.gemini_provider import GeminiProviderSurface
from patchproof.hard_mode import (
    HardModeCaseKind,
    HardModeConfigurationError,
    HardModePacingPolicy,
    HardModeProtocol,
    HardModeRepositoryCache,
    _model_call_budget_preflight,
    _pace_between_cases,
    load_hard_mode_manifest,
    render_summary_markdown,
    run_live,
    summarize_live,
)
from patchproof.install_strategy import (
    ContractSynthesisError,
    InstallPlan,
    InstallStrategy,
    synthesize_contract,
)
from patchproof.models import EnvironmentReadiness, EnvironmentReadinessStatus

_PROJECT_ROOT = Path(__file__).parents[1]
_MANIFEST_PATH = _PROJECT_ROOT / "benchmarks" / "hard_mode" / "manifest.json"
_RESULTS_ROOT = _MANIFEST_PATH.parent / "results"


def _manifest_with_available_calls(available: int):
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    payload = manifest.model_dump(mode="json")
    payload["protocol"]["candidate_calls_per_case"] = 3
    updated_protocol = HardModeProtocol.model_validate(payload["protocol"])
    logical_required = updated_protocol.derive_maximum_possible_logical_model_calls(
        case_count=len(manifest.cases)
    )
    provider_required = updated_protocol.derive_maximum_possible_provider_calls(
        case_count=len(manifest.cases)
    )
    payload["protocol"].update(
        {
            "provider_surface": GeminiProviderSurface.GEMINI_DEVELOPER_API,
            "maximum_possible_logical_model_calls": logical_required,
            "maximum_possible_provider_calls": provider_required,
            "declared_available_provider_calls": available,
            "model_call_budget_preflight_passed": available >= provider_required,
        }
    )
    return hard_mode_module.HardModeManifest.model_validate(payload)


class _FakeCandidateGenerator:
    def __init__(self) -> None:
        self.attempts = [
            SimpleNamespace(
                sequence=sequence,
                validated=SimpleNamespace(artifact=f"artifact-{sequence}"),
            )
            for sequence in range(1, 5)
        ]
        self.model_calls = 0
        self.repair_feedback: list[object] = []

    async def generate_initial(self):
        self.model_calls += 1
        return self.attempts[0]

    async def repair(self, *, feedback):
        self.repair_feedback.append(feedback)
        self.model_calls += 1
        return self.attempts[self.model_calls - 1]


class _FakeChallengeSession:
    def __init__(self, statuses: list[hard_mode_module.MechanicalEvidenceStatus]) -> None:
        self.statuses = iter(statuses)
        self.artifacts: list[str] = []

    def run(self, *, artifact):
        self.artifacts.append(artifact)
        return SimpleNamespace(assessment=SimpleNamespace(mechanical_status=next(self.statuses)))


def _candidate_sequences(
    statuses: list[hard_mode_module.MechanicalEvidenceStatus],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], _FakeCandidateGenerator, _FakeChallengeSession]:
    generator = _FakeCandidateGenerator()
    session = _FakeChallengeSession(statuses)
    monkeypatch.setattr(
        hard_mode_module.EvidenceWorkflow,
        "_repair_feedback",
        staticmethod(
            lambda *, attempt, challenge: (
                attempt.sequence,
                challenge.assessment.mechanical_status,
            )
        ),
    )

    async def collect() -> list[int]:
        return [
            attempt.sequence
            async for attempt, _challenge in hard_mode_module._bounded_candidate_challenges(
                generator=generator,
                session=session,
            )
        ]

    return asyncio.run(collect()), generator, session


def test_historical_candidate_budget_matches_production() -> None:
    from patchproof.test_generation import _MAX_CANDIDATE_MODEL_CALLS, _MAX_REPAIRS

    assert hard_mode_module._MAX_CANDIDATE_MODEL_CALLS == _MAX_CANDIDATE_MODEL_CALLS == 3
    assert hard_mode_module._MAX_REPAIRS == _MAX_REPAIRS == 2


def test_initial_discrimination_stops_before_any_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequences, generator, session = _candidate_sequences(
        [hard_mode_module.MechanicalEvidenceStatus.DISCRIMINATING],
        monkeypatch,
    )

    assert sequences == [1]
    assert generator.model_calls == 1
    assert generator.repair_feedback == []
    assert session.artifacts == ["artifact-1"]


def test_first_repair_discrimination_prevents_second_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequences, generator, session = _candidate_sequences(
        [
            hard_mode_module.MechanicalEvidenceStatus.NON_DISCRIMINATING,
            hard_mode_module.MechanicalEvidenceStatus.DISCRIMINATING,
        ],
        monkeypatch,
    )

    assert sequences == [1, 2]
    assert generator.model_calls == 2
    assert generator.repair_feedback == [
        (1, hard_mode_module.MechanicalEvidenceStatus.NON_DISCRIMINATING)
    ]
    assert session.artifacts == ["artifact-1", "artifact-2"]


def test_non_discrimination_reaches_second_repair_but_never_a_fourth_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequences, generator, session = _candidate_sequences(
        [hard_mode_module.MechanicalEvidenceStatus.NON_DISCRIMINATING] * 3,
        monkeypatch,
    )

    assert sequences == [1, 2, 3]
    assert generator.model_calls == 3
    assert generator.repair_feedback == [
        (1, hard_mode_module.MechanicalEvidenceStatus.NON_DISCRIMINATING),
        (2, hard_mode_module.MechanicalEvidenceStatus.NON_DISCRIMINATING),
    ]
    assert session.artifacts == ["artifact-1", "artifact-2", "artifact-3"]


def test_hard_mode_manifest_freezes_four_historical_repositories_and_one_synthetic() -> None:
    manifest, digest = load_hard_mode_manifest(_MANIFEST_PATH)

    historical = [case for case in manifest.cases if case.kind is HardModeCaseKind.HISTORICAL_PR]
    synthetic = [case for case in manifest.cases if case.kind is HardModeCaseKind.LOCAL_SYNTHETIC]
    assert len(historical) == 4
    assert len({case.repository for case in historical}) == 4
    assert len(synthetic) == 1
    assert len(digest) == 64
    assert all(case.interface_exists_on_both_revisions for case in manifest.cases)
    assert all(case.expected_base_result == "ASSERTION_FAILED" for case in manifest.cases)
    assert all(case.expected_head_result == "PASSED" for case in manifest.cases)


def test_synthetic_fixture_bootstraps_to_the_frozen_commits(
    writable_test_directory: Path,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    case = next(item for item in manifest.cases if item.kind is HardModeCaseKind.LOCAL_SYNTHETIC)
    repository = HardModeRepositoryCache(
        writable_test_directory / "cache",
        _MANIFEST_PATH.parent,
    ).prepare(case)

    assert repository.is_dir()
    assert (repository / ".git").is_dir()


def test_historical_challenge_uses_the_symmetric_probed_install_plan(
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    case = next(item for item in manifest.cases if item.kind is HardModeCaseKind.HISTORICAL_PR)
    install = InstallPlan(
        strategy=InstallStrategy.UV_PIP_EDITABLE,
        commands=(
            ("uv", "venv"),
            ("uv", "pip", "install", "-e", "."),
            ("uv", "pip", "install", "pytest"),
        ),
        rationale="installable project with no separately declared test requirements",
    )
    contract = synthesize_contract(install)
    reader = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        hard_mode_module,
        "DeterministicContextRetriever",
        lambda **_kwargs: reader,
    )

    def resolve(**kwargs):
        captured.update(kwargs)
        return contract, install, install

    monkeypatch.setattr(hard_mode_module, "resolve_contract_for_pair", resolve)

    plan = hard_mode_module._execution_plan(Path.cwd(), case)
    challenge = hard_mode_module._challenge(
        Path.cwd(),
        writable_test_directory,
        case,
        plan=plan,
    )

    assert captured["base_sha"] == case.base_sha
    assert captured["head_sha"] == case.head_sha
    assert captured["prober"].reader is reader
    assert plan.base_install == plan.head_install == install
    assert challenge.runner.contract == contract
    assert challenge.runner.install_dependencies is True


def test_historical_execution_plan_fails_closed_when_pair_cannot_be_synthesized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    case = next(item for item in manifest.cases if item.kind is HardModeCaseKind.HISTORICAL_PR)
    monkeypatch.setattr(
        hard_mode_module,
        "DeterministicContextRetriever",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        hard_mode_module,
        "resolve_contract_for_pair",
        lambda **_kwargs: (_ for _ in ()).throw(
            ContractSynthesisError("BASE and HEAD imply different install strategies")
        ),
    )

    with pytest.raises(HardModeConfigurationError, match="no safe symmetric install plan"):
        hard_mode_module._execution_plan(Path.cwd(), case)


def test_live_case_checks_environment_before_any_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    case = next(item for item in manifest.cases if item.kind is HardModeCaseKind.HISTORICAL_PR)
    context_json = "{}"
    context = SimpleNamespace(
        model_dump_json=lambda: context_json,
        model_dump=lambda **_kwargs: {},
    )

    class Retriever:
        def __init__(self, **_kwargs) -> None:
            pass

        def retrieve(self, **_kwargs):
            return context

    class Session:
        def prepare_environment(self):
            return EnvironmentReadiness(
                status=EnvironmentReadinessStatus.BASE_SETUP_FAILED,
                reason="bounded setup failure",
            )

    class Challenge:
        @staticmethod
        def session(**_kwargs):
            class Manager:
                def __enter__(self):
                    return Session()

                def __exit__(self, *_args):
                    return False

            return Manager()

    monkeypatch.setattr(hard_mode_module, "DeterministicContextRetriever", Retriever)
    monkeypatch.setattr(
        hard_mode_module,
        "_execution_plan",
        lambda *_args: SimpleNamespace(
            contract=None,
            install_dependencies=True,
            base_install=None,
            head_install=None,
        ),
    )
    monkeypatch.setattr(hard_mode_module, "_execution_plan_document", lambda _plan: {})
    monkeypatch.setattr(hard_mode_module, "_challenge", lambda *_args, **_kwargs: Challenge())
    monkeypatch.setattr(
        hard_mode_module,
        "BehavioralClaimAgent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("model construction must not occur before readiness")
        ),
    )

    result = asyncio.run(
        hard_mode_module._run_live_case(
            case=case,
            repository=Path.cwd(),
            workspace_root=Path.cwd(),
            model_name="gemini-3.6-flash",
            provider_config=SimpleNamespace(),
            expected_context_sha256=hashlib.sha256(context_json.encode()).hexdigest(),
        )
    )

    assert result["terminal_status"] == "ENVIRONMENT_NOT_READY"
    assert result["candidate_attempts"] == []


def test_summary_uses_raw_denominators_and_handles_failed_model_calls() -> None:
    raw = {
        "declared_run_id": "hard-test",
        "model_name": "gemini-3.6-flash",
        "cases": [
            {
                "kind": "HISTORICAL_PR",
                "claim_result": {
                    "selection": {"claim": {"claim_id": "claim-one"}},
                    "usage": {
                        "total_tokens": 100,
                        "duration_seconds": 1.0,
                        "provider_attempts": 1,
                    },
                },
                "candidate_attempts": [
                    {
                        "sequence": 1,
                        "origin": "INITIAL",
                        "status": "VALIDATED",
                        "usage": {
                            "total_tokens": 200,
                            "duration_seconds": 2.0,
                            "provider_attempts": 1,
                        },
                    }
                ],
                "candidate_evaluations": [
                    {
                        "attempt_sequence": 1,
                        "mechanical_status": "DISCRIMINATING",
                        "matches_hidden_oracle_direction": True,
                    }
                ],
                "semantic_assessment": {
                    "usage": {
                        "total_tokens": 50,
                        "duration_seconds": 0.5,
                        "provider_attempts": 1,
                    }
                },
                "terminal_status": "CLAIM_SUPPORTED_FOR_SCENARIO",
                "final_outcome": "CLAIM_SUPPORTED_FOR_SCENARIO",
            },
            {
                "kind": "LOCAL_SYNTHETIC",
                "claim_result": None,
                "candidate_attempts": [],
                "candidate_evaluations": [],
                "semantic_assessment": None,
                "terminal_status": "CLAIM_INVOCATION_ERROR",
                "final_outcome": None,
                "error": {
                    "usage": {
                        "total_tokens": 25,
                        "reasoning_tokens": 9,
                        "duration_seconds": 0.25,
                        "provider_attempts": 1,
                    }
                },
            },
        ],
    }

    summary = summarize_live(raw)

    assert summary["case_count"] == 2
    assert summary["claim_selected_count"] == 1
    assert summary["discriminating_initial_candidate_count"] == 1
    assert summary["incorrect_support_count"] == 0
    assert summary["total_tokens"] == 375
    assert summary["reasoning_tokens"] == 9
    assert summary["logical_model_result_count"] == 4


def test_predeclared_inter_case_pacing_is_uniform_and_journaled(
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    protocol = HardModeProtocol.model_validate(
        {
            **manifest.protocol.model_dump(mode="json"),
            "pacing_policy": HardModePacingPolicy.BETWEEN_CASES,
            "inter_case_delay_seconds": 60.0,
        }
    )
    sleep_calls: list[float] = []
    clock = iter((10.0, 70.25))
    monkeypatch.setattr("patchproof.hard_mode.time.sleep", sleep_calls.append)
    monkeypatch.setattr("patchproof.hard_mode.time.perf_counter", lambda: next(clock))
    journal = writable_test_directory / "pacing.jsonl"

    _pace_between_cases(
        protocol=protocol,
        journal_path=journal,
        completed_case_id="case-one",
        next_case_id="case-two",
    )

    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert sleep_calls == [60.0]
    assert [event["event"] for event in events] == [
        "INTER_CASE_PACING_STARTED",
        "INTER_CASE_PACING_COMPLETED",
    ]
    assert events[1]["declared_delay_seconds"] == 60.0
    assert events[1]["actual_delay_seconds"] == 60.25


def test_model_call_budget_is_derived_from_protocol_and_case_count() -> None:
    manifest = _manifest_with_available_calls(50)

    logical_required = manifest.protocol.derive_maximum_possible_logical_model_calls(
        case_count=len(manifest.cases)
    )
    provider_required = manifest.protocol.derive_maximum_possible_provider_calls(
        case_count=len(manifest.cases)
    )
    preflight = _model_call_budget_preflight(manifest)

    assert logical_required == 5 * (1 + 3 + 1) == 25
    assert manifest.protocol.maximum_provider_attempts_per_logical_call() == 1 + 1 == 2
    assert provider_required == logical_required * (1 + 1) == 50
    assert manifest.protocol.maximum_possible_logical_model_calls == 25
    assert manifest.protocol.maximum_possible_provider_calls == 50
    assert manifest.protocol.declared_available_provider_calls == 50
    assert manifest.protocol.model_call_budget_preflight_passed is True
    assert preflight.maximum_possible_logical_model_calls == logical_required
    assert preflight.maximum_possible_provider_calls == provider_required
    assert preflight.declared_available_provider_calls == 50
    assert preflight.passed is True


def test_zero_transient_retries_make_provider_calls_equal_logical_calls() -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    protocol = HardModeProtocol.model_validate(
        {
            **manifest.protocol.model_dump(mode="json"),
            "candidate_calls_per_case": 3,
            "transient_provider_retries_per_logical_call": 0,
        }
    )

    logical = protocol.derive_maximum_possible_logical_model_calls(case_count=len(manifest.cases))
    provider = protocol.derive_maximum_possible_provider_calls(case_count=len(manifest.cases))

    assert protocol.maximum_provider_attempts_per_logical_call() == 1
    assert provider == logical == 25


def test_live_run_refuses_insufficient_declared_calls_before_creating_journal(
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest_with_available_calls(49)
    monkeypatch.setattr(
        hard_mode_module,
        "load_hard_mode_manifest",
        lambda _path: (manifest, "a" * 64),
    )
    journal = writable_test_directory / "insufficient.jsonl"
    raw = writable_test_directory / "insufficient.json"

    with pytest.raises(HardModeConfigurationError, match=r"49.*below.*50"):
        run_live(
            manifest_path=writable_test_directory / "manifest.json",
            cache_root=writable_test_directory / "cache",
            workspace_root=writable_test_directory / "workspaces",
            gate_path=writable_test_directory / "missing-gate.json",
            journal_path=journal,
            raw_path=raw,
        )

    assert not journal.exists()
    assert not raw.exists()
    assert not (writable_test_directory / "cache").exists()


def test_old_manifest_requires_current_candidate_budget_for_a_new_live_run(
    writable_test_directory: Path,
) -> None:
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)
    assert manifest.protocol.declared_available_provider_calls is None
    assert manifest.protocol.provider_surface is None
    journal = writable_test_directory / "old-manifest.jsonl"

    with pytest.raises(HardModeConfigurationError, match="candidate-call budget of 3"):
        run_live(
            manifest_path=_MANIFEST_PATH,
            cache_root=writable_test_directory / "cache",
            workspace_root=writable_test_directory / "workspaces",
            gate_path=writable_test_directory / "missing-gate.json",
            journal_path=journal,
            raw_path=writable_test_directory / "raw.json",
        )

    assert not journal.exists()


def test_live_run_records_passing_call_budget_before_fake_case_execution(
    writable_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest_with_available_calls(50)
    manifest_sha256 = "a" * 64
    monkeypatch.setattr(
        hard_mode_module,
        "load_hard_mode_manifest",
        lambda _path: (manifest, manifest_sha256),
    )
    monkeypatch.setattr(
        hard_mode_module.HardModeRepositoryCache,
        "prepare",
        lambda _self, _case: writable_test_directory,
    )

    async def fake_live_case(**kwargs):
        case = kwargs["case"]
        return {
            "case_id": case.case_id,
            "kind": str(case.kind),
            "terminal_status": "FAKE_NO_PROVIDER_CALL",
            "claim_result": None,
            "candidate_attempts": [],
            "candidate_evaluations": [],
            "semantic_assessment": None,
            "final_outcome": None,
        }

    monkeypatch.setattr(hard_mode_module, "_run_live_case", fake_live_case)
    gate = writable_test_directory / "gate.json"
    gate.write_text(
        json.dumps(
            {
                "accepted": True,
                "manifest_sha256": manifest_sha256,
                "case_count": len(manifest.cases),
                "cases": [
                    {
                        "case_id": case.case_id,
                        "accepted": True,
                        "anti_leakage": {"context_sha256": "b" * 64},
                    }
                    for case in manifest.cases
                ],
            }
        ),
        encoding="utf-8",
    )
    journal = writable_test_directory / "passing.jsonl"
    raw_path = writable_test_directory / "passing.json"

    raw = run_live(
        manifest_path=writable_test_directory / "manifest.json",
        cache_root=writable_test_directory / "cache",
        workspace_root=writable_test_directory / "workspaces",
        gate_path=gate,
        journal_path=journal,
        raw_path=raw_path,
    )

    started = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert started["event"] == "RUN_STARTED"
    assert started["provider_surface"] == "GEMINI_DEVELOPER_API"
    assert started["maximum_possible_logical_model_calls"] == 25
    assert started["maximum_possible_provider_calls"] == 50
    assert started["declared_available_provider_calls"] == 50
    assert started["model_call_budget_preflight_passed"] is True
    assert raw["maximum_possible_logical_model_calls"] == 25
    assert raw["maximum_possible_provider_calls"] == 50
    assert raw["declared_available_provider_calls"] == 50
    assert raw["model_call_budget_preflight_passed"] is True
    assert raw["provider_surface"] == "GEMINI_DEVELOPER_API"
    summary = summarize_live(raw)
    assert summary["maximum_possible_logical_model_calls"] == 25
    assert summary["maximum_possible_provider_calls"] == 50
    assert summary["declared_available_provider_calls"] == 50
    assert summary["model_call_budget_preflight_passed"] is True
    assert summary["provider_surface"] == "GEMINI_DEVELOPER_API"
    rendered = render_summary_markdown(summary)
    assert "logical model call is one semantic PatchProof task" in rendered
    assert "provider attempt is an actual provider" in rendered
    assert "including any permitted transient retry" in rendered


def test_existing_journal_blocks_any_live_rerun(writable_test_directory: Path) -> None:
    journal = writable_test_directory / "live_journal.jsonl"
    journal.write_text('{"event":"RUN_STARTED"}\n', encoding="utf-8")

    with pytest.raises(HardModeConfigurationError, match="already started"):
        run_live(
            manifest_path=_MANIFEST_PATH,
            cache_root=writable_test_directory / "cache",
            workspace_root=writable_test_directory / "workspaces",
            gate_path=writable_test_directory / "gate.json",
            journal_path=journal,
            raw_path=writable_test_directory / "raw.json",
        )


def test_checked_in_results_match_the_frozen_manifest_gate_and_summary() -> None:
    manifest_bytes = _MANIFEST_PATH.read_bytes()
    gate_bytes = (_RESULTS_ROOT / "oracle_gate.json").read_bytes()
    raw = json.loads((_RESULTS_ROOT / "raw.json").read_text(encoding="utf-8"))
    summary = json.loads((_RESULTS_ROOT / "summary.json").read_text(encoding="utf-8"))
    manifest, _ = load_hard_mode_manifest(_MANIFEST_PATH)

    assert raw["manifest_sha256"] == hashlib.sha256(manifest_bytes).hexdigest()
    assert raw["oracle_gate_sha256"] == hashlib.sha256(gate_bytes).hexdigest()
    assert [case["case_id"] for case in raw["cases"]] == [case.case_id for case in manifest.cases]
    assert summary == summarize_live(raw)
