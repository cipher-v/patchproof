"""Static contracts for the checked-in Google Cloud deployment configuration."""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_cloud_deploy_defaults_to_isolated_final_firestore_namespace() -> None:
    script = (_ROOT / "deploy" / "gcp" / "deploy.ps1").read_text(encoding="utf-8")

    assert '[string]$FirestoreNamespace = "patchproof-final-v1"' in script
    assert "$FirestoreNamespace -cnotmatch '^[a-z][a-z0-9_-]{1,39}$'" in script
    assert script.count("PATCHPROOF_FIRESTORE_NAMESPACE=${FirestoreNamespace}") == 1


def test_cloud_deployment_docs_distinguish_historical_and_final_namespaces() -> None:
    documentation = (_ROOT / "docs" / "23_CLOUD_ANALYZE_INTEGRATION.md").read_text(encoding="utf-8")

    assert "`patchproof` namespace contains historical and pre-final evidence" in documentation
    assert "`patchproof-final-v1` Firestore collection namespace" in documentation
    assert "No records are migrated or copied between namespaces." in documentation
