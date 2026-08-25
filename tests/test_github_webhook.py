"""Unit tests for the GitHub authentication boundary."""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime

import pytest

from patchproof.github_webhook import (
    GitHubPullRequestWebhook,
    validate_delivery_id,
    verify_github_signature,
)


def _signature(secret: bytes, body: bytes) -> str:
    return f"sha256={hmac.new(secret, body, hashlib.sha256).hexdigest()}"


def test_signature_verification_accepts_only_the_exact_authenticated_bytes() -> None:
    secret = b"phase-two-secret"
    body = b'{"action":"opened"}'

    assert verify_github_signature(
        secret=secret,
        body=body,
        signature_header=_signature(secret, body),
    )
    assert not verify_github_signature(
        secret=secret,
        body=body + b" ",
        signature_header=_signature(secret, body),
    )


@pytest.mark.parametrize(
    "signature",
    [None, "", "sha1=" + "a" * 40, "sha256=short", "sha256=" + "g" * 64],
)
def test_signature_verification_rejects_missing_or_malformed_headers(
    signature: str | None,
) -> None:
    assert not verify_github_signature(
        secret=b"secret",
        body=b"payload",
        signature_header=signature,
    )


@pytest.mark.parametrize("delivery", [None, "", "contains spaces", "x" * 129, "bad/slash"])
def test_delivery_id_validation_rejects_unbounded_or_ambiguous_values(
    delivery: str | None,
) -> None:
    with pytest.raises(ValueError, match="Delivery"):
        validate_delivery_id(delivery)


def test_delivery_id_validation_preserves_a_valid_github_identifier() -> None:
    assert validate_delivery_id(" 123e4567-e89b-12d3-a456-426614174000 ") == (
        "123e4567-e89b-12d3-a456-426614174000"
    )


def test_pull_request_event_title_collapses_internal_whitespace() -> None:
    webhook = GitHubPullRequestWebhook.model_validate(
        {
            "action": "opened",
            "number": 1,
            "repository": {"full_name": "cipher-v/patchproof", "private": False},
            "pull_request": {
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
                "updated_at": datetime(2026, 8, 25, tzinfo=UTC),
                "title": "  Normalize   this\n title  ",
            },
            "installation": {"id": 156402136},
        }
    )

    event = webhook.to_event("phase8-live-proof")

    assert event.title == "Normalize this title"
