"""Install strategies must be derived from committed files and never from a model.

Regression coverage for `docs/18_INDEPENDENT_ARCHITECTURE_REVIEW.md` section B.3: the
execution contract previously admitted only `uv sync --frozen`, which requires a
committed `uv.lock` at both revisions. No repository in the sealed unseen holdout
satisfies that, so dependency installation had only ever been exercised against
PatchProof's own repository.
"""

from __future__ import annotations

import pytest

from patchproof.execution_contract import ExecutionContract
from patchproof.install_strategy import (
    SYNTHESIZED_TEST_PATH,
    ContractSynthesisError,
    DependencyInstallProber,
    InstallPlan,
    InstallStrategy,
    resolve_contract_for_pair,
    synthesize_contract,
    validate_probed_install_command,
)


class FakeRevisionReader:
    """Serve a fixed committed file set and contents per revision."""

    def __init__(self, revisions: dict[str, dict[str, bytes]]) -> None:
        self.revisions = revisions
        self.reads: list[tuple[str, str]] = []

    def committed_paths(self, revision_sha: str, *, max_paths: int = 20_000) -> frozenset[str]:
        return frozenset(self.revisions[revision_sha])

    def read_committed_file(self, *, revision_sha: str, path: str, max_bytes: int = 8_192) -> bytes:
        self.reads.append((revision_sha, path))
        try:
            return self.revisions[revision_sha][path]
        except KeyError as error:
            raise OSError(f"missing committed path {path!r}") from error


def _prober(files: dict[str, bytes]) -> tuple[DependencyInstallProber, FakeRevisionReader]:
    reader = FakeRevisionReader({"a" * 40: files})
    return DependencyInstallProber(reader=reader), reader


def test_committed_lockfile_reproduces_the_locked_environment() -> None:
    prober, _ = _prober({"uv.lock": b"", "pyproject.toml": b"[project]\nname='x'\n"})
    plan = prober.probe("a" * 40)
    assert plan.strategy is InstallStrategy.UV_SYNC_LOCKED
    assert plan.commands == (("uv", "sync", "--frozen", "--all-groups"),)


def test_declared_test_extra_is_used_when_the_repository_declares_it() -> None:
    prober, _ = _prober(
        {
            "pyproject.toml": (
                b"[project]\nname='x'\n\n[project.optional-dependencies]\n"
                b"test = ['pytest']\ndocs = ['sphinx']\n"
            )
        }
    )
    plan = prober.probe("a" * 40)
    assert plan.strategy is InstallStrategy.UV_PIP_EDITABLE_WITH_TEST_EXTRA
    assert ("uv", "pip", "install", "-e", ".[test]") in plan.commands


def test_pep735_dependency_group_is_preferred_over_an_extra() -> None:
    prober, _ = _prober(
        {
            "pyproject.toml": (
                b"[project]\nname='x'\n\n[project.optional-dependencies]\ndev = ['pytest']\n"
                b"\n[dependency-groups]\ntest = ['pytest']\n"
            )
        }
    )
    plan = prober.probe("a" * 40)
    assert ("uv", "pip", "install", "--group", "test") in plan.commands


def test_undeclared_extras_are_never_invented() -> None:
    """A project with no test extra must not be installed with a guessed one."""
    prober, _ = _prober({"pyproject.toml": b"[project]\nname='x'\n"})
    plan = prober.probe("a" * 40)
    assert plan.strategy is InstallStrategy.UV_PIP_EDITABLE
    assert all("[" not in token for command in plan.commands for token in command)


def test_committed_requirements_file_is_selected_deterministically() -> None:
    prober, _ = _prober(
        {
            "setup.py": b"",
            "requirements.txt": b"",
            "requirements-test.txt": b"",
            "requirements-docs.txt": b"",
        }
    )
    plan = prober.probe("a" * 40)
    assert plan.strategy is InstallStrategy.UV_PIP_EDITABLE_WITH_REQUIREMENTS
    assert ("uv", "pip", "install", "-r", "requirements-test.txt") in plan.commands


def test_repository_without_packaging_metadata_is_unsupported_not_guessed() -> None:
    prober, _ = _prober({"README.md": b"", "module.py": b""})
    plan = prober.probe("a" * 40)
    assert plan.strategy is InstallStrategy.UNSUPPORTED
    assert plan.commands == ()
    assert not plan.supported


def test_malformed_manifest_degrades_instead_of_failing_the_run() -> None:
    prober, _ = _prober({"pyproject.toml": b"this is not [valid toml", "setup.py": b""})
    plan = prober.probe("a" * 40)
    assert plan.supported


def test_probe_reads_only_packaging_metadata() -> None:
    prober, reader = _prober(
        {
            "pyproject.toml": b"[project]\nname='x'\n",
            "tests/test_secret_oracle.py": b"assert False",
            "src/app.py": b"",
        }
    )
    prober.probe("a" * 40)
    assert [path for _, path in reader.reads] == ["pyproject.toml"]


@pytest.mark.parametrize(
    "command",
    [
        ("uv", "pip", "install", "requests"),
        ("uv", "pip", "install", "-r", "/etc/passwd"),
        ("uv", "pip", "install", "-r", "../../secrets.txt"),
        ("uv", "pip", "install", "-e", ".[test]; rm -rf /"),
        ("sh", "-c", "echo hi"),
        ("uv", "run", "--", "python", "-c", "print(1)"),
    ],
)
def test_non_allowlisted_install_tokens_are_rejected(command: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        validate_probed_install_command(command)


def test_synthesized_contract_validates_through_the_same_model() -> None:
    plan = InstallPlan(
        strategy=InstallStrategy.UV_PIP_EDITABLE,
        commands=(
            ("uv", "venv"),
            ("uv", "pip", "install", "-e", "."),
            ("uv", "pip", "install", "pytest"),
        ),
        rationale="test",
    )
    contract = synthesize_contract(plan)
    assert isinstance(contract, ExecutionContract)
    assert contract.synthesized is True
    assert contract.allowed_test_paths == (SYNTHESIZED_TEST_PATH,)
    assert contract.test.command == ("python", "-m", "pytest")


def test_repository_declared_contract_cannot_use_probed_commands() -> None:
    """Hand-written `.patchproof.yaml` keeps the narrow literal allowlist."""
    with pytest.raises(ValueError):
        ExecutionContract(
            version=1,
            python="3.12",
            install=(("uv", "pip", "install", "-e", "."),),
            test={"command": ["python", "-m", "pytest"]},
            allowed_test_paths=("tests/generated/",),
            timeout_seconds=120,
        )


def test_unsupported_revision_cannot_be_synthesized() -> None:
    plan = InstallPlan(strategy=InstallStrategy.UNSUPPORTED, commands=(), rationale="nothing")
    with pytest.raises(ContractSynthesisError):
        synthesize_contract(plan)


def test_divergent_base_and_head_install_strategies_abstain() -> None:
    """A PR that changes how the project installs is not a comparable pair."""
    reader = FakeRevisionReader(
        {
            "b" * 40: {"setup.py": b"", "requirements.txt": b""},
            "h" * 40: {"uv.lock": b"", "pyproject.toml": b"[project]\nname='x'\n"},
        }
    )
    with pytest.raises(ContractSynthesisError, match="different install strategies"):
        resolve_contract_for_pair(
            prober=DependencyInstallProber(reader=reader),
            base_sha="b" * 40,
            head_sha="h" * 40,
        )


def test_matching_base_and_head_yield_one_shared_contract() -> None:
    files = {"pyproject.toml": b"[project]\nname='x'\n\n[dependency-groups]\ntest = ['pytest']\n"}
    reader = FakeRevisionReader({"b" * 40: dict(files), "h" * 40: dict(files)})
    contract, base_plan, head_plan = resolve_contract_for_pair(
        prober=DependencyInstallProber(reader=reader),
        base_sha="b" * 40,
        head_sha="h" * 40,
    )
    assert base_plan.commands == head_plan.commands == contract.install
    assert contract.synthesized is True
