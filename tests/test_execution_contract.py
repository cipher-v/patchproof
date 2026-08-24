"""Tests for the strict repository-owned `.patchproof.yaml` boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from patchproof.execution_contract import (
    ExecutionContract,
    ExecutionContractError,
    ExecutionContractLoader,
)


def _valid_document() -> dict:
    return {
        "version": 1,
        "python": "3.12",
        "install": [["uv", "sync", "--frozen"]],
        "test": {"command": ["python", "-m", "pytest"]},
        "allowed_test_paths": ["tests/patchproof_generated/"],
        "timeout_seconds": 120,
    }


def test_repository_contract_loads_and_resolves_python_without_a_shell() -> None:
    contract = ExecutionContractLoader().load(Path(__file__).parents[1])

    assert contract.python == "3.12"
    assert contract.install == (("uv", "sync", "--frozen", "--all-groups"),)
    assert contract.test.command == ("python", "-m", "pytest")
    assert contract.permits_test_path("tests/patchproof_generated/test_claim.py")
    assert not contract.permits_test_path("tests/test_claim.py")
    command = contract.resolved_test_command(python_executable=Path("python.exe"))
    assert command[-2:] == ("-m", "pytest")
    assert command[0].endswith("python.exe")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("python", "3.14"),
        ("install", [["pip", "install", "-r", "requirements.txt"]]),
        ("install", ["uv sync --frozen"]),
        ("test", {"command": "uv run pytest"}),
        ("test", {"command": ["sh", "-c", "pytest"]}),
        ("allowed_test_paths", ["../tests/"]),
        ("allowed_test_paths", ["tests"]),
        ("timeout_seconds", 301),
    ],
)
def test_contract_rejects_unsupported_versions_commands_and_paths(field: str, value) -> None:
    document = _valid_document()
    document[field] = value

    with pytest.raises(ValueError):
        ExecutionContract.model_validate(document)


def test_contract_rejects_unknown_fields() -> None:
    document = _valid_document()
    document["shell"] = "powershell"

    with pytest.raises(ValueError):
        ExecutionContract.model_validate(document)


def test_loader_fails_closed_for_missing_malformed_and_oversized_contracts(
    writable_test_directory: Path,
) -> None:
    loader = ExecutionContractLoader(max_bytes=100)
    with pytest.raises(ExecutionContractError, match="could not be read"):
        loader.load(writable_test_directory)

    contract_path = writable_test_directory / ".patchproof.yaml"
    contract_path.write_text("[not: valid", encoding="utf-8")
    with pytest.raises(ExecutionContractError, match="not valid"):
        loader.load(writable_test_directory)

    contract_path.write_text("x" * 101, encoding="utf-8")
    with pytest.raises(ExecutionContractError, match="oversized"):
        loader.load(writable_test_directory)
