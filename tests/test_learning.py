"""Judged outcomes feed the next plan, across replay days; and one viewer's state has one writer at a time."""
import json
import shutil
from datetime import timedelta

import pytest

from helpers import MINI, NOW, read_jsonl
from viewer_agent.judge import run_judge
from viewer_agent.replay import run_replay, write_jsonl
from viewer_agent.store import StaleState
from viewer_agent.times import iso, parse_ts

DAY = timedelta(days=4)


def _later_day(tmp_path):
    """The mini pack's replay, four days later, with new event ids."""
    pack = tmp_path / "pack_day2"
    shutil.copytree(MINI, pack)
    times = ("occurred_at", "sent_at", "received_at", "deliver_at")
    rows = [{**t, "event_id": "d2-" + t["event_id"], "session_id": "d2-" + t["session_id"],
             **{k: iso(parse_ts(t[k]) + DAY) for k in times}} for t in read_jsonl(MINI / "replay" / "triggers.jsonl")]
    rows.append({**rows[0], "event_id": "d2-trg-R1", "user_id": "U-R1", "session_id": "d2-s-R1"})
    write_jsonl(pack / "replay" / "triggers.jsonl", rows)
    write_jsonl(pack / "replay" / "live_events.jsonl", [])
    write_jsonl(pack / "replay" / "control_events.jsonl", [])
    return pack


def test_a_goal_that_failed_on_day_one_is_not_repeated_on_day_two(tmp_path):
    day1 = tmp_path / "day1"
    pack1 = tmp_path / "pack_day1"
    shutil.copytree(MINI, pack1)
    t = read_jsonl(MINI / "replay" / "triggers.jsonl")[0]
    write_jsonl(pack1 / "replay" / "triggers.jsonl",
                read_jsonl(MINI / "replay" / "triggers.jsonl") + [{**t, "event_id": "trg-R1", "user_id": "U-R1", "session_id": "s-R1-2"}])
    run_replay(pack1, day1)
    first = next(d for d in read_jsonl(day1 / "decisions.jsonl") if d["viewer_id"] == "U-R1")
    assert first["goal"]["id"] == "G07"

    # nothing happened for U-R1 over the next days: the judge marks G07 as failed
    (day1 / "sim").mkdir()
    late = iso(NOW + timedelta(days=3, hours=1))
    write_jsonl(day1 / "sim" / "outcome_events.jsonl", [
        {"event_id": "sim-x", "user_id": "U-A1", "event": "app_open", "occurred_at": late, "sent_at": late,
         "received_at": late, "session_id": "x", "app_dialect": "d1", "app_language": None, "platform": None,
         "app_version": None, "properties": {}}])
    run_judge(pack1, day1)
    outcome = next(o for o in read_jsonl(day1 / "outcomes.jsonl") if o["user_id"] == "U-R1")
    assert outcome["verdict"] == "failed"

    day2 = tmp_path / "day2"
    run_replay(_later_day(tmp_path), day2, state_db=day1 / "state.db")
    second = next(d for d in read_jsonl(day2 / "decisions.jsonl") if d["viewer_id"] == "U-R1")
    assert (second.get("goal") or {}).get("id") != "G07"
    assert any(a.startswith("learned: G07 failed") for a in second["assumptions"])
    day1_ids = {a["action_id"] for a in read_jsonl(day1 / "actions.jsonl")}
    assert not day1_ids & {a["action_id"] for a in read_jsonl(day2 / "actions.jsonl")}  # ids continue, no repeats


def test_a_second_writer_cannot_overwrite_newer_state(env):
    env.store.save_state("U-X", {"user_id": "U-X", "goal_history": []}, NOW)
    first = env.store.load_state("U-X")
    second = json.loads(json.dumps(first))
    env.store.save_state("U-X", first, NOW)  # the first writer wins
    with pytest.raises(StaleState):
        env.store.save_state("U-X", second, NOW)  # the second read an older version
    assert env.store.load_state("U-X")["version"] == 2
