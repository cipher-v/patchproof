from __future__ import annotations

from uuid import UUID

import pytest

import patchproof.cli as cli_module
from patchproof.cloud_client import CloudAnalyzeReceipt, CloudClientError, PatchProofCloudClient
from patchproof.dashboard import DashboardFailure, load_demo_snapshot

_RUN_ID = UUID("695eaa20-7db3-492f-a57e-9819ebb54087")
_PR_URL = "https://github.com/python-jsonschema/jsonschema/pull/1208"


class FakeCloudClient:
    def __init__(self, *, run=None, error: Exception | None = None) -> None:
        self.run = run or load_demo_snapshot().runs[0].model_copy(update={"status": "COMPLETE"})
        self.error = error
        self.submitted = []

    def submit(self, pr_url: str) -> CloudAnalyzeReceipt:
        self.submitted.append(pr_url)
        if self.error is not None:
            raise self.error
        return CloudAnalyzeReceipt(
            run_id=_RUN_ID,
            status="ACCEPTED",
            pr_url=pr_url,
            dashboard_url=f"https://control.example/dashboard?run={_RUN_ID}",
            result_url=f"https://control.example/api/runs/{_RUN_ID}",
        )

    def wait_for_terminal(self, run_id, *, on_status):
        assert run_id == _RUN_ID
        on_status(self.run.status)
        return self.run


def install_cloud(monkeypatch, fake: FakeCloudClient) -> None:
    monkeypatch.setattr(
        cli_module,
        "PatchProofCloudClient",
        lambda *, control_url: fake if control_url == "https://control.example" else None,
    )


def test_cloud_is_default_when_control_url_is_configured(monkeypatch, capsys) -> None:
    fake = FakeCloudClient()
    install_cloud(monkeypatch, fake)
    monkeypatch.setenv("PATCHPROOF_CONTROL_URL", "https://control.example")
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda *_args, **_kwargs: pytest.fail("cloud mode silently fell back to local"),
    )

    exit_code = cli_module.main(["analyze", _PR_URL])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert fake.submitted == [_PR_URL]
    assert "Mode: CLOUD" in output
    assert f"Run ID: {_RUN_ID}" in output
    assert "def test_github_pull_request_webhook_collapses_title_whitespace" in output
    assert "CLAIM_SUPPORTED_FOR_SCENARIO" in output


def test_local_is_default_without_control_url(monkeypatch, capsys) -> None:
    calls = []
    monkeypatch.delenv("PATCHPROOF_CONTROL_URL", raising=False)
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda pr_url, *, output_root: calls.append((pr_url, output_root)),
    )

    exit_code = cli_module.main(["analyze", _PR_URL])

    assert exit_code == 0
    assert calls == [(_PR_URL, None)]
    assert "Mode: LOCAL" in capsys.readouterr().out


def test_explicit_local_overrides_configured_cloud(monkeypatch) -> None:
    calls = []
    monkeypatch.setenv("PATCHPROOF_CONTROL_URL", "https://control.example")
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda pr_url, *, output_root: calls.append((pr_url, output_root)),
    )

    assert cli_module.main(["analyze", "--local", _PR_URL]) == 0
    assert calls == [(_PR_URL, None)]


def test_explicit_cloud_requires_configuration_and_never_falls_back(monkeypatch, capsys) -> None:
    monkeypatch.delenv("PATCHPROOF_CONTROL_URL", raising=False)
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda *_args, **_kwargs: pytest.fail("requested cloud mode fell back to local"),
    )

    exit_code = cli_module.main(["analyze", "--cloud", _PR_URL])

    assert exit_code == 2
    assert "no local fallback was attempted" in capsys.readouterr().err


def test_cloud_failure_does_not_fall_back(monkeypatch, capsys) -> None:
    fake = FakeCloudClient(error=CloudClientError("control unavailable"))
    install_cloud(monkeypatch, fake)
    monkeypatch.setenv("PATCHPROOF_CONTROL_URL", "https://control.example")
    monkeypatch.setattr(
        cli_module,
        "analyze_known_pr",
        lambda *_args, **_kwargs: pytest.fail("cloud failure fell back to local"),
    )

    assert cli_module.main(["analyze", _PR_URL]) == 2
    assert "control unavailable" in capsys.readouterr().err


def test_cloud_abstention_is_success_but_infrastructure_failure_is_nonzero(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PATCHPROOF_CONTROL_URL", "https://control.example")
    abstention = load_demo_snapshot().runs[1].model_copy(update={"status": "ABSTAINED"})
    install_cloud(monkeypatch, FakeCloudClient(run=abstention))
    assert cli_module.main(["analyze", _PR_URL]) == 0
    assert "ABSTAINED" in capsys.readouterr().out

    failed = abstention.model_copy(
        update={
            "status": "FAILED",
            "failure": DashboardFailure(
                phase="CONTEXT",
                error_code="WORKSPACE_FAILED",
                summary="The executor failed closed.",
                retryable=True,
            ),
        }
    )
    install_cloud(monkeypatch, FakeCloudClient(run=failed))
    assert cli_module.main(["analyze", _PR_URL]) == 2
    assert "WORKSPACE_FAILED" in capsys.readouterr().out


def test_analyze_mode_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args(["analyze", "--local", "--cloud", _PR_URL])


def test_cloud_polling_reports_changes_and_stops_at_terminal(monkeypatch) -> None:
    client = PatchProofCloudClient(control_url="https://control.example")
    running = load_demo_snapshot().runs[0].model_copy(update={"status": "RUNNING_BASE_HEAD"})
    complete = running.model_copy(update={"status": "COMPLETE"})
    responses = iter((running, complete))
    monkeypatch.setattr(client, "get_run", lambda _run_id: next(responses))
    monkeypatch.setattr("patchproof.cloud_client.time.sleep", lambda _seconds: None)
    statuses = []

    result = client.wait_for_terminal(
        _RUN_ID,
        poll_interval_seconds=0.01,
        timeout_seconds=1,
        on_status=statuses.append,
    )

    assert result.status == "COMPLETE"
    assert statuses == ["RUNNING_BASE_HEAD", "COMPLETE"]


def test_dashboard_command_prints_remote_url_and_can_skip_browser(monkeypatch, capsys) -> None:
    opened = []
    monkeypatch.setenv("PATCHPROOF_CONTROL_URL", "https://control.example")
    monkeypatch.setattr(cli_module.webbrowser, "open", opened.append)

    assert cli_module.main(["dashboard", "--no-open"]) == 0
    assert "https://control.example/dashboard" in capsys.readouterr().out
    assert opened == []


def test_dashboard_local_starts_preview_without_module_knowledge(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.update(app=app, **kwargs))

    assert cli_module.main(["dashboard", "--local", "--no-open", "--port", "8123"]) == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123
