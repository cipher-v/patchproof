"""PatchProof command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
import webbrowser
from pathlib import Path

from patchproof.cloud_client import CloudClientError, PatchProofCloudClient, print_cloud_run
from patchproof.dashboard_preview import build_preview_app
from patchproof.pr_analyze import PrAnalyzeError, analyze_known_pr


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patchproof",
        description="Generate claim-scoped executable evidence for GitHub pull requests.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    analyze = commands.add_parser(
        "analyze",
        help="analyze a GitHub PR and show Gemini-generated test evidence",
    )
    analyze.add_argument("pr_url", help="canonical GitHub pull-request URL")
    mode = analyze.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true", help="force the local known-case analyzer")
    mode.add_argument("--cloud", action="store_true", help="require the configured cloud backend")
    analyze.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="artifact root (default: .patchproof/runs)",
    )
    analyze.add_argument(
        "--debug",
        action="store_true",
        help="show a traceback for unexpected product-layer failures",
    )
    dashboard = commands.add_parser(
        "dashboard",
        help="open the deployed Evidence Console or run its local preview",
    )
    dashboard.add_argument(
        "--local",
        action="store_true",
        help="serve the deterministic local Evidence Console preview",
    )
    dashboard.add_argument(
        "--no-open",
        action="store_true",
        help="print the dashboard URL without opening a browser",
    )
    dashboard.add_argument("--port", type=int, default=8092, help="local preview port")
    return parser


def _control_url() -> str:
    return os.environ.get("PATCHPROOF_CONTROL_URL", "").strip().rstrip("/")


def _run_cloud_analyze(args: argparse.Namespace, *, control_url: str) -> int:
    if args.output_root is not None:
        raise CloudClientError("--output-root applies only to --local analysis")
    client = PatchProofCloudClient(control_url=control_url)
    print("PatchProof")
    print("Mode: CLOUD")
    receipt = client.submit(args.pr_url)
    print(f"Run ID: {receipt.run_id}")
    print(f"Dashboard: {receipt.dashboard_url}")
    print(f"Status API: {receipt.result_url}")
    run = client.wait_for_terminal(
        receipt.run_id,
        on_status=lambda value: print(f"Cloud status: {value}", flush=True),
    )
    print_cloud_run(run)
    return 2 if run.status == "FAILED" else 0


def _run_dashboard(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65_535:
        raise PrAnalyzeError("dashboard port must be between 1 and 65535")
    if args.local:
        import uvicorn

        url = f"http://127.0.0.1:{args.port}/dashboard"
        print(f"PatchProof local Evidence Console: {url}")
        if not args.no_open:
            webbrowser.open(url)
        uvicorn.run(build_preview_app(), host="127.0.0.1", port=args.port)
        return 0
    control_url = _control_url()
    if not control_url:
        raise PrAnalyzeError("set PATCHPROOF_CONTROL_URL or use 'patchproof dashboard --local'")
    url = f"{control_url}/dashboard"
    print(f"PatchProof Evidence Console: {url}")
    if not args.no_open:
        webbrowser.open(url)
    return 0


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        if args.command == "analyze":
            control_url = _control_url()
            use_cloud = args.cloud or (bool(control_url) and not args.local)
            if args.cloud and not control_url:
                raise CloudClientError(
                    "--cloud requires PATCHPROOF_CONTROL_URL; no local fallback was attempted"
                )
            if use_cloud:
                return _run_cloud_analyze(args, control_url=control_url)
            print("Mode: LOCAL")
            analyze_known_pr(args.pr_url, output_root=args.output_root)
            return 0
        if args.command == "dashboard":
            return _run_dashboard(args)
    except (PrAnalyzeError, CloudClientError) as error:
        print(f"PatchProof: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"PatchProof: unexpected product failure: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        if getattr(args, "debug", False):
            traceback.print_exc()
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
