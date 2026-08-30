"""Safety tests for the explicitly operated legacy Check Run helper."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "supersede_legacy_checks.py"
_SPEC = importlib.util.spec_from_file_location("patchproof_legacy_check_cleanup", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_CLEANUP = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CLEANUP
_SPEC.loader.exec_module(_CLEANUP)

_APP_ID = _CLEANUP._APP_ID
_APP_SLUG = _CLEANUP._APP_SLUG
_LEGACY_CHECKS = _CLEANUP._LEGACY_CHECKS
_SUMMARY = _CLEANUP._SUMMARY
_TITLE = _CLEANUP._TITLE
_payload = _CLEANUP._payload
_validate_remote_check = _CLEANUP._validate_remote_check
main = _CLEANUP.main


def _remote_document(sequence: int = 0) -> dict[str, object]:
    check = _LEGACY_CHECKS[sequence]
    return {
        "id": check.check_run_id,
        "name": "PatchProof claim evidence",
        "status": "completed",
        "conclusion": "success",
        "head_sha": check.head_sha,
        "external_id": check.external_id,
        "app": {"id": _APP_ID, "slug": _APP_SLUG},
    }


def test_helper_is_dry_run_by_default(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0

    output = capsys.readouterr().out
    assert "Dry run only" in output
    assert all(str(check.check_run_id) in output for check in _LEGACY_CHECKS)


def test_payload_is_neutral_and_uses_explicit_legacy_notice() -> None:
    payload = _payload(_LEGACY_CHECKS[0])

    assert payload.conclusion == "neutral"
    assert payload.output.title == _TITLE
    assert payload.output.title.startswith("Legacy PatchProof evidence — superseded")
    assert payload.output.summary == _SUMMARY


@pytest.mark.parametrize("field", ["id", "name", "status", "head_sha", "external_id"])
def test_remote_identity_drift_fails_closed(field: str) -> None:
    document = _remote_document()
    document[field] = "unexpected"

    with pytest.raises(RuntimeError, match="pinned identity"):
        _validate_remote_check(_LEGACY_CHECKS[0], document)


def test_remote_app_drift_fails_closed() -> None:
    document = _remote_document()
    document["app"] = {"id": _APP_ID, "slug": "different-app"}

    with pytest.raises(RuntimeError, match="pinned GitHub App"):
        _validate_remote_check(_LEGACY_CHECKS[0], document)
