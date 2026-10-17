"""Real-time behaviour under load: a shared worker queue, the latency budget, and traffic surges."""
import shutil
from datetime import timedelta

from helpers import MINI, read_jsonl
from viewer_agent.config import Settings
from viewer_agent.replay import Replay, SurgeMonitor, write_jsonl
from viewer_agent.times import iso, parse_ts

VIEWERS = ["U-A1", "U-A2", "U-S1", "U-R1", "U-C1", "U-O1", "U-P1", "U-M1", "U-D1", "U-T1"]


def _burst_pack(tmp_path, spacing_s=1.0):
    """The mini pack with one live open per viewer, all within a few seconds of each other."""
    pack = tmp_path / "pack"
    shutil.copytree(MINI, pack)
    base = read_jsonl(MINI / "replay" / "triggers.jsonl")[0]
    start = parse_ts("2026-10-01T14:00:00.000Z")
    triggers = []
    for i in range(7):  # a quiet hour before the burst: one holdout open every 10 minutes
        at = iso(start - timedelta(minutes=70 - 10 * i))
        triggers.append(dict(base, event_id=f"quiet-{i}", user_id="U-H1", session_id=f"quiet-{i}", occurred_at=at,
                             sent_at=at, received_at=at, deliver_at=at))
    for i, user in enumerate(VIEWERS):
        at = iso(start + timedelta(seconds=i * spacing_s))
        triggers.append(dict(base, event_id=f"burst-{i}", user_id=user, session_id=f"burst-{i}", occurred_at=at,
                             sent_at=at, received_at=at, deliver_at=at))
    write_jsonl(pack / "replay" / "triggers.jsonl", triggers)
    write_jsonl(pack / "replay" / "control_events.jsonl", [])
    write_jsonl(pack / "replay" / "live_events.jsonl", [])
    return pack


def test_a_queue_forms_when_workers_are_busy_and_the_budget_still_holds(tmp_path):
    # one worker and a slow model: each open waits for the one before it
    settings = Settings(workers=1, mock_model_latency_s=8.0, surge_min_opens=1000)
    replay = Replay(_burst_pack(tmp_path), tmp_path / "out", settings=settings)
    summary = replay.run()
    decisions = read_jsonl(tmp_path / "out" / "decisions.jsonl")
    waits = [d["queue_wait_ms"] for d in decisions if d["trigger"]["event_id"].startswith("burst")]
    assert waits[0] == 0 and max(waits) > 5_000
    for d in decisions:  # the wait is part of the decision time and of the publish time
        assert d["decided_in_ms"] >= d["queue_wait_ms"]
    # once little budget is left, the agent stops asking the model instead of running late
    assert any("time budget left" in (d["model"].get("skipped") or "") for d in decisions)
    assert summary["latency"]["failed"] == 0


def test_more_workers_remove_the_queue(tmp_path):
    settings = Settings(workers=8, mock_model_latency_s=8.0, surge_min_opens=1000)
    Replay(_burst_pack(tmp_path), tmp_path / "out", settings=settings).run()
    assert max(d["queue_wait_ms"] for d in read_jsonl(tmp_path / "out" / "decisions.jsonl")) == 0


def test_surge_is_noticed_and_optional_model_calls_are_shed(tmp_path):
    settings = Settings(surge_min_opens=5, surge_ratio=2.5)
    summary = Replay(_burst_pack(tmp_path), tmp_path / "out", settings=settings).run()
    assert summary["traffic_surges"] and summary["traffic_surges"][0]["opens_in_10_min"] >= 5
    decisions = read_jsonl(tmp_path / "out" / "decisions.jsonl")
    shed = [d for d in decisions if d.get("during_surge") and d["arm"] == "agent" and d["status"] == "PUBLISHED"]
    assert shed and all(d["model"]["calls"] == 0 and "surge" in d["model"]["skipped"] for d in shed)


def test_surge_monitor_compares_against_the_previous_hour():
    monitor = SurgeMonitor(Settings(surge_min_opens=10, surge_ratio=2.5))
    t = parse_ts("2026-10-01T13:00:00.000Z")
    for i in range(60):  # steady: 10 opens per 10 minutes for an hour
        assert monitor.add(t + timedelta(minutes=i)) is False
    burst = [monitor.add(t + timedelta(minutes=60, seconds=s)) for s in range(0, 600, 20)]  # 30 more in 10 min
    assert burst[-1] is True and monitor.windows


def test_capacity_check_shows_how_many_workers_a_burst_needs():
    from viewer_agent.replay import capacity_check
    start = parse_ts("2026-10-01T14:00:00.000Z")
    # 120 opens over an hour, each taking 2 s to decide
    decisions = [{"trigger": {"delivered_at": iso(start + timedelta(seconds=30 * i))}, "decided_in_ms": 2000,
                  "queue_wait_ms": 0} for i in range(120)]
    cap = capacity_check(decisions, budget_s=60)
    assert cap["day_at_3x_traffic"]["1_workers"]["max_wait_ms"] == 0          # 10 s apart, 2 s each: no queue
    burst = cap["1000_opens_in_one_minute"]
    assert burst["4_workers"]["over_budget"] > 0                               # 1,000 x 2 s / 4 = 500 s of work
    assert burst["32_workers"]["over_budget"] == 0                             # 62.5 s of work spread over a minute
