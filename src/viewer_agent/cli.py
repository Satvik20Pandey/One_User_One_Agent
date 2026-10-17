"""Command line entry point.

    python -m viewer_agent run-all --pack ./pack --out sample_run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--pack", default="pack", help="root of the data pack (default: ./pack)")
    p.add_argument("--out", default="sample_run", help="output directory (default: ./sample_run)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="viewer_agent", description="One User, One Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("replay", help="run the agent over the replay day and write decisions/actions")
    _common(p)
    p.add_argument("--provider", choices=["mock", "openai"], default="mock",
                   help="model provider; mock replays the scripted faults and needs no API key")
    p.add_argument("--limit", type=int, default=None, help="stop after N triggers (for quick checks)")
    p.add_argument("--continue-from", default=None,
                   help="state.db of an earlier, judged run: viewers keep their goals, outcomes and learnings")

    p = sub.add_parser("simulate", help="run the pack's outcome simulator on the published actions")
    _common(p)
    p.add_argument("--seed", default="oua-practice")

    p = sub.add_parser("judge", help="judge every goal from the simulated outcomes and export viewer state")
    _common(p)

    p = sub.add_parser("impact", help="write impact_report.md and final_report.md")
    _common(p)

    p = sub.add_parser("inspect", help="show what the agent knew, decided and observed for one viewer")
    _common(p)
    p.add_argument("user_id")

    p = sub.add_parser("run-all", help="replay, simulate, judge and impact in one go")
    _common(p)
    p.add_argument("--provider", choices=["mock", "openai"], default="mock")
    p.add_argument("--seed", default="oua-practice")
    return parser


def cmd_replay(args) -> None:
    from .replay import run_replay
    previous = getattr(args, "continue_from", None)
    summary = run_replay(Path(args.pack), Path(args.out), provider_name=args.provider, limit=getattr(args, "limit", None),
                         state_db=Path(previous) if previous else None)
    print(json.dumps(summary, indent=2))


def cmd_simulate(args) -> None:
    script = Path(args.pack) / "simulator" / "simulate_outcomes.py"
    out = Path(args.out)
    cmd = [sys.executable, str(script), "--pack", str(args.pack), "--actions", str(out / "actions.jsonl"),
           "--out", str(out / "sim"), "--seed", args.seed]
    subprocess.run(cmd, check=True)


def cmd_judge(args) -> None:
    from .judge import run_judge
    summary = run_judge(Path(args.pack), Path(args.out))
    print(json.dumps(summary, indent=2))


def cmd_impact(args) -> None:
    from .judge import write_reports
    paths = write_reports(Path(args.pack), Path(args.out))
    print("\n".join(str(p) for p in paths))


def cmd_inspect(args) -> None:
    from .judge import inspect_viewer
    print(json.dumps(inspect_viewer(Path(args.out), args.user_id), indent=2, ensure_ascii=False))


def cmd_run_all(args) -> None:
    cmd_replay(args)
    cmd_simulate(args)
    cmd_judge(args)
    cmd_impact(args)


COMMANDS = {"replay": cmd_replay, "simulate": cmd_simulate, "judge": cmd_judge, "impact": cmd_impact,
            "inspect": cmd_inspect, "run-all": cmd_run_all}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    COMMANDS[args.command](args)
