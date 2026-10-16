"""The command line, driven the way an evaluator would drive it (minus the pack simulator, which lives in the pack)."""
import json

from helpers import MINI
from viewer_agent.cli import build_parser, main


def test_every_subcommand_parses():
    parser = build_parser()
    for argv in (["replay", "--pack", "p", "--out", "o", "--provider", "mock", "--limit", "5"],
                 ["simulate", "--pack", "p", "--out", "o", "--seed", "s"],
                 ["judge"], ["impact"], ["inspect", "U-0001"],
                 ["run-all", "--pack", "p", "--out", "o", "--provider", "openai", "--seed", "x"]):
        args = parser.parse_args(argv)
        assert args.command == argv[0]


def test_replay_judge_impact_inspect_through_main(tmp_path, capsys):
    out = tmp_path / "out"
    main(["replay", "--pack", str(MINI), "--out", str(out), "--limit", "3"])
    assert json.loads(capsys.readouterr().out)["triggers"] == 3

    (out / "sim").mkdir()
    (out / "sim" / "outcome_events.jsonl").write_text("", encoding="utf-8")
    (out / "sim" / "simulator_report.json").write_text(
        json.dumps({"seed": "t", "events_written": 0, "counts": {}, "policy_problem_count": 0}), encoding="utf-8")

    main(["judge", "--pack", str(MINI), "--out", str(out)])
    assert json.loads(capsys.readouterr().out)["goals_judged"] >= 1
    main(["impact", "--pack", str(MINI), "--out", str(out)])
    assert "impact_report.md" in capsys.readouterr().out
    assert (out / "impact_report.md").exists() and (out / "final_report.md").exists()

    main(["inspect", "U-A1", "--out", str(out)])
    shown = json.loads(capsys.readouterr().out)
    assert shown["state"]["user_id"] == "U-A1" and shown["decisions"]
