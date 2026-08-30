"""PatchProof command-line interface."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

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
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        if args.command == "analyze":
            analyze_known_pr(args.pr_url, output_root=args.output_root)
            return 0
    except PrAnalyzeError as error:
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
