"""Safely mark two pinned pre-final PatchProof Check Runs as superseded."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from patchproof.github_checks import (
    CHECK_NAME,
    GITHUB_API_VERSION,
    GitHubAppInstallationTokenProvider,
    GitHubCheckOutput,
    GitHubCheckPayload,
    GitHubChecksClient,
)

_REPOSITORY = "cipher-v/patchproof"
_APP_ID = 4_711_074
_APP_SLUG = "cipherv-patchproof"
_INSTALLATION_ID = 156_402_136
_TITLE = "Legacy PatchProof evidence — superseded"
_SUMMARY = (
    "This Check Run predates the current hardened PatchProof evidence policy and has been "
    "superseded. It should not be interpreted as current PatchProof evidence."
)


@dataclass(frozen=True, slots=True)
class LegacyCheck:
    check_run_id: int
    head_sha: str
    external_id: str


_LEGACY_CHECKS = (
    LegacyCheck(
        check_run_id=97_764_451_438,
        head_sha="745283fedf88ebb7b1e038b3893e152cee89687a",
        external_id="695eaa20-7db3-492f-a57e-9819ebb54087",
    ),
    LegacyCheck(
        check_run_id=99_159_359_877,
        head_sha="9230446b50d4f715a5f0909f9f55415f46368b3f",
        external_id="539b83f3-227c-433c-abe5-17c08f018de0",
    ),
)


def _payload(check: LegacyCheck) -> GitHubCheckPayload:
    return GitHubCheckPayload(
        head_sha=check.head_sha,
        conclusion="neutral",
        external_id=check.external_id,
        output=GitHubCheckOutput(
            title=_TITLE,
            summary=_SUMMARY,
            text=(
                f"{_SUMMARY}\n\n"
                f"Legacy Check Run: `{check.check_run_id}`. The historical record is retained; "
                "no Firestore evidence was deleted or rewritten."
            ),
        ),
    )


def _validate_remote_check(check: LegacyCheck, document: dict[str, Any]) -> None:
    expected = {
        "id": check.check_run_id,
        "name": CHECK_NAME,
        "status": "completed",
        "head_sha": check.head_sha,
        "external_id": check.external_id,
    }
    actual = {name: document.get(name) for name in expected}
    if actual != expected:
        raise RuntimeError(f"Check Run {check.check_run_id} no longer matches its pinned identity")
    app = document.get("app")
    if not isinstance(app, dict) or app.get("id") != _APP_ID or app.get("slug") != _APP_SLUG:
        raise RuntimeError(f"Check Run {check.check_run_id} is not owned by the pinned GitHub App")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run or explicitly supersede two pinned legacy PatchProof Check Runs."
    )
    parser.add_argument(
        "--private-key-file",
        type=Path,
        help="Path to the existing PatchProof GitHub App PEM; required only with --apply.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the two guarded PATCH requests. Without this flag, only print the plan.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    for check in _LEGACY_CHECKS:
        print(
            f"Check Run {check.check_run_id}: conclusion=neutral; title={_TITLE!r}; "
            f"repository={_REPOSITORY}"
        )
    if not args.apply:
        print("Dry run only. Re-run with --private-key-file <PEM> --apply to update GitHub.")
        return 0
    if args.private_key_file is None or not args.private_key_file.is_file():
        raise SystemExit("--apply requires an existing --private-key-file")

    provider = GitHubAppInstallationTokenProvider(
        app_id=_APP_ID,
        private_key_pem=args.private_key_file.read_text(encoding="utf-8"),
    )
    token = provider.token_for(installation_id=_INSTALLATION_ID, repository=_REPOSITORY)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    with httpx.Client(timeout=20.0) as client:
        checks = GitHubChecksClient(tokens=provider, client=client)
        for check in _LEGACY_CHECKS:
            response = client.get(
                f"https://api.github.com/repos/{_REPOSITORY}/check-runs/{check.check_run_id}",
                headers=headers,
            )
            if not 200 <= response.status_code < 300:
                raise RuntimeError(
                    f"GitHub rejected the Check Run identity read with HTTP {response.status_code}"
                )
            document = response.json()
            if not isinstance(document, dict):
                raise RuntimeError("GitHub returned an unusable Check Run identity document")
            _validate_remote_check(check, document)
            payload = _payload(check)
            if (
                document.get("conclusion") == "neutral"
                and document.get("output", {}).get("title") == _TITLE
                and document.get("output", {}).get("summary") == _SUMMARY
            ):
                print(f"Check Run {check.check_run_id}: already superseded; unchanged")
                continue
            remote_id = checks.upsert_completed(
                repository=_REPOSITORY,
                installation_id=_INSTALLATION_ID,
                payload=payload,
                known_check_run_id=check.check_run_id,
            )
            if remote_id != check.check_run_id:
                raise RuntimeError("GitHub returned a different Check Run ID")
            print(f"Check Run {remote_id}: updated to neutral legacy notice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
