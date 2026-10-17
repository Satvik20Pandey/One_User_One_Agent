"""Evaluators run a second, unseen replay day. Nothing in the agent may depend on 2026-10-01 or on the exact
faults and control events of the practice day."""
import json
import shutil
from datetime import timedelta

from helpers import MINI, read_jsonl
from viewer_agent.judge import run_judge, write_reports
from viewer_agent.replay import run_replay, write_jsonl
from viewer_agent.times import iso, parse_ts

SHIFT = timedelta(days=9)


def _shift(row: dict, keys) -> dict:
    return {**row, **{k: iso(parse_ts(row[k]) + SHIFT) for k in keys if row.get(k)}}


def test_a_shifted_day_with_new_fault_and_control_types(tmp_path):
    pack = tmp_path / "pack"
    shutil.copytree(MINI, pack)
    replay = pack / "replay"
    times = ("occurred_at", "sent_at", "received_at", "deliver_at")
    write_jsonl(replay / "triggers.jsonl", [_shift(t, times) for t in read_jsonl(replay / "triggers.jsonl")])
    write_jsonl(replay / "live_events.jsonl", [_shift(e, times) for e in read_jsonl(replay / "live_events.jsonl")])
    controls = [_shift(c, ("at", "until")) for c in read_jsonl(replay / "control_events.jsonl")]
    controls.append({"at": controls[0]["at"], "type": "catalogue_refreshed", "detail": "a type we have never seen"})
    write_jsonl(replay / "control_events.jsonl", controls)
    faults = read_jsonl(replay / "model_faults.jsonl")
    faults.append({"trigger_event_id": "trg-S1-1", "user_id": "U-S1", "fault": "http_500"})  # no model_output
    write_jsonl(replay / "model_faults.jsonl", faults)

    out = tmp_path / "out"
    summary = run_replay(pack, out)
    assert summary["replay_day_ist"] == "2026-10-10"
    assert summary["latency"]["failed"] == 0
    s1 = next(d for d in read_jsonl(out / "decisions.jsonl") if d["viewer_id"] == "U-S1")
    assert s1["status"] == "PUBLISHED" and "http_500" in s1["model"]["failure"]
    assert any(c["type"] == "catalogue_refreshed" for c in summary["control_events"])
    assert json.loads((out / "latency_report.json").read_text())["decisions"] == summary["triggers"]

    # and the rest of the pipeline copes with the unknown event too
    (out / "sim").mkdir()
    write_jsonl(out / "sim" / "outcome_events.jsonl", [])
    (out / "sim" / "simulator_report.json").write_text(
        json.dumps({"seed": "x", "events_written": 0, "counts": {}, "policy_problem_count": 0}))
    run_judge(pack, out)
    write_reports(pack, out)
    final = (out / "final_report.md").read_text(encoding="utf-8")
    assert "Replay day 2026-10-10" in final and "`catalogue_refreshed` received" in final
