"""
CLI entrypoint for demo orchestration.

Usage:
    python -m app.demo                         # run all scenarios
    python -m app.demo happy_path_booking      # run one scenario by ID
    python -m app.demo --list                  # list available scenarios
    python -m app.demo --output ./my_output    # custom output directory

Requires the API server to be running (uvicorn app.main:app).
For in-process execution (no server needed), use the test suite or
import DemoOrchestrator directly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.demo.orchestrator import DemoOrchestrator, save_artifacts
from app.demo.scenarios import load_scenarios


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.demo",
        description="Run Maison Éclat demo scenarios against the local API.",
    )
    parser.add_argument(
        "scenario_id",
        nargs="?",
        help="Run a specific scenario by ID (omit to run all)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenario IDs and exit",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--output",
        default="demo_output",
        help="Output directory for artifacts (default: demo_output)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-turn output; only show final summary",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    orch = DemoOrchestrator(base_url=args.base_url)

    if args.scenario_id:
        results = [await orch.run_scenario(args.scenario_id)]
    else:
        results = await orch.run_all()

    all_pass = True
    for result in results:
        paths = save_artifacts(result, output_dir=args.output)

        if not args.quiet:
            print(result.to_summary())
            print(f"  📄 Transcript: {paths['transcript']}")
            print(f"  📝 Summary:    {paths['summary']}")
            print()

        if not result.success:
            all_pass = False

    # Final banner
    total = len(results)
    passed = sum(1 for r in results if r.success)
    failed = total - passed
    print(f"{'=' * 50}")
    print(f"Demo run complete: {passed}/{total} scenarios passed")
    if failed:
        print(f"  ❌ {failed} scenario(s) had assertion failures")
    else:
        print(f"  ✅ All scenarios passed!")
    print(f"Artifacts saved to: {args.output}/")

    return 0 if all_pass else 1


def main() -> None:
    args = _parse_args()

    if args.list:
        scenarios = load_scenarios()
        print("Available demo scenarios:")
        for s in scenarios:
            print(f"  {s.id:30s} — {s.title}")
            print(f"    {s.description}")
            print(f"    Tags: {', '.join(s.tags)}")
            print()
        return

    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
