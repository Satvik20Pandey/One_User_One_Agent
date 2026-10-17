"""End to end on the mini pack: replay, control events, judge and reports."""
import json
from datetime import timedelta

from helpers import MINI, NOW, read_jsonl
from viewer_agent.judge import run_judge, write_reports
from viewer_agent.replay import run_replay, write_jsonl
from viewer_agent.times import iso


def test_mini_replay_end_to_end(tmp_path):
    out = tmp_path / "out"
    summary = run_replay(MINI, out)
    decisions = read_jsonl(out / "decisions.jsonl")
    actions = read_jsonl(out / "actions.jsonl")
    by_user = {d["viewer_id"]: d for d in decisions if d["status"] != "DUPLICATE"}

    assert summary["triggers"] == 7
    assert sum(1 for d in decisions if d["status"] == "DUPLICATE") == 1
    assert by_user["U-H1"]["status"] == "HOLDOUT_NO_ACTION"
    assert all(a["user_id"] != "U-H1" for a in actions)

    # a live purchase closed U-M1's first-paid goal before its deadline
    assert any(n["user_id"] == "U-M1" for n in summary["plan_revisions_from_live_events"])

    # the withdrawn title was taken out of every live page
    withdrawn = next(c for c in summary["control_events"] if c["type"] == "title_withdrawn")
    republished = [a for a in actions if a.get("replaces")]
    assert len(republished) == len(withdrawn["live_pages_republished"])
    for page in republished:
        assert "T0003" not in [page["hero_title_id"]] + [t for r in page["rows"] for t in r["title_ids"]]

    # U-A2 opened during the model outage, U-C1 during the degraded store window
    assert by_user["U-A2"]["fallback_used"] == "model_unavailable"
    assert by_user["U-C1"]["budget"]["simulated_wait_ms"] > 0
    assert summary["latency"]["late"] == 0 and summary["latency"]["failed"] == 0
    assert (out / "latency_report.json").exists()

    # judge and reports from a tiny hand-made outcome stream
    page_id = next(a["action_id"] for a in actions if a["user_id"] == "U-A1" and a["type"] == "home_page")
    (out / "sim").mkdir()
    write_jsonl(out / "sim" / "outcome_events.jsonl", [
        _outcome("U-A1", "home_page_shown", NOW + timedelta(seconds=40), {"personalised": True, "page_action_id": page_id}),
        _outcome("U-A1", "episode_completed", NOW + timedelta(days=1), {"title_id": "T0001", "episode_id": "rava-d1-s1e1"}),
    ])
    (out / "sim" / "simulator_report.json").write_text(json.dumps(
        {"seed": "test", "events_written": 2, "counts": {}, "policy_problem_count": 0}))

    judged = run_judge(MINI, out)
    assert judged["goals_judged"] >= 5
    outcome = next(o for o in read_jsonl(out / "outcomes.jsonl") if o["user_id"] == "U-A1")
    assert outcome["verdict"] == "succeeded" and outcome["attribution"] == "exposed_before_success"
    assert (out / "viewer_state" / "U-A1.json").exists()

    for path in write_reports(MINI, out):
        assert path.exists()
    assert "holdout" in (out / "impact_report.md").read_text(encoding="utf-8")


def _outcome(user_id, event, at, props):
    return {"event_id": f"sim-{user_id}-{event}", "user_id": user_id, "event": event, "occurred_at": iso(at),
            "sent_at": iso(at), "received_at": iso(at), "session_id": "sim", "app_dialect": "d1",
            "app_language": None, "platform": None, "app_version": None, "properties": props}
