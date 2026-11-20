"""Command line entry point for orderwitness."""

from __future__ import annotations

import argparse
import json
import sys

from .core import OrderWitnessError, audit, write_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orderwitness",
        description=(
            "Find order-dependent pytest failures and shrink them to a "
            "minimal polluter -> victim witness."
        ),
    )
    parser.add_argument("--root", default=".", help="project root (default: .)")
    parser.add_argument(
        "--shuffles", type=int, default=8, help="number of seeded shuffles (default: 8)"
    )
    parser.add_argument("--seed", type=int, default=0, help="first shuffle seed")
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="per-pytest-run timeout seconds"
    )
    parser.add_argument(
        "--max-runs", type=int, default=120, help="budget for the shrink phase"
    )
    parser.add_argument(
        "--stop-on-first",
        action="store_true",
        help="stop scanning after the first failing shuffle",
    )
    parser.add_argument("--output", help="also write the JSON report to this path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit(
            args.root,
            shuffles=max(1, args.shuffles),
            seed=args.seed,
            timeout=args.timeout,
            max_runs=max(1, args.max_runs),
            stop_on_first=args.stop_on_first,
        )
    except OrderWitnessError as exc:
        payload = {
            "tool": "orderwitness",
            "status": "error",
            "reason": str(exc),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"orderwitness: {exc}", file=sys.stderr)
        return 2

    write_report(report, args.output)
    if report.witness:
        kind = report.witness["kind"]
        print(
            f"orderwitness: {kind} witness in {report.witness['subset_size']} test(s); "
            f"repro: {report.witness['repro']}",
            file=sys.stderr,
        )
        return 1
    print(
        f"orderwitness: no order dependence in {report.shuffles_run} shuffle(s)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())