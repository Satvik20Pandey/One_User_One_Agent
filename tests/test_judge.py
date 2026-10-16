"""Closing the loop: verdicts from attributable signals only, and honest attribution."""
from datetime import timedelta

from helpers import NOW
from viewer_agent.judge import _exposure
from viewer_agent.times import iso


def _goal(goal_id, hours=72, **context):
    return {"goal_id": goal_id, "instance_id": "G-1", "name": goal_id, "rule_version": "v1",
            "started_at": iso(NOW), "deadline": iso(NOW + timedelta(hours=hours)), "context": context}


def _event(event, at, **props):
    return {"event": event, "_at": at, "event_id": f"{event}@{at.isoformat()}", "properties": props}


def test_success_counts_only_inside_the_window(env):
    goal = _goal("G02")
    done = dict(title_id="T0004", episode_id="hapi-d1-s1e1")
    before = _event("episode_completed", NOW - timedelta(hours=1), **done)
    inside = _event("episode_completed", NOW + timedelta(hours=5), **done)
    after = _event("episode_completed", NOW + timedelta(hours=80), **done)
    end = NOW + timedelta(days=4)

    assert env.rules.judge(goal, [before], end, env.pack).verdict == "failed"
    verdict = env.rules.judge(goal, [before, inside], end, env.pack)
    assert verdict.verdict == "succeeded" and verdict.success_event_id == inside["event_id"]
    assert env.rules.judge(goal, [after], end, env.pack).verdict == "failed"


def test_open_window_is_inconclusive_not_failed(env):
    verdict = env.rules.judge(_goal("G02"), [], NOW + timedelta(hours=10), env.pack)
    assert verdict.verdict == "inconclusive"


def test_attribution_requires_exposure_before_success():
    goal = _goal("G05", hours=168)
    ours = [{"action_id": "A-1", "goal_id": "G05", "type": "home_page", "published_at": iso(NOW)}]
    shown = _event("home_page_shown", NOW + timedelta(minutes=5), personalised=True, page_action_id="A-1")

    assert _exposure(goal, [shown], ours, iso(NOW + timedelta(minutes=20)))["attribution"] == "exposed_before_success"
    assert _exposure(goal, [shown], ours, iso(NOW + timedelta(minutes=2)))["attribution"] == "success_before_exposure"
    assert _exposure(goal, [], ours, iso(NOW + timedelta(minutes=20)))["attribution"] == "published_but_never_seen"
    assert _exposure(goal, [shown], ours, None)["attribution"] == "exposed_no_success"
    assert _exposure(goal, [shown], [], None)["attribution"] == "no_action_taken"
    assert _exposure({**goal, "shadow": True}, [], [], None)["attribution"] == "holdout_no_action"


def test_habit_goal_needs_three_distinct_days(env):
    goal = _goal("G09", hours=336)
    plays = [_event("play_started", NOW + timedelta(days=d, hours=2), title_id="T0001") for d in (1, 1, 2)]
    end = NOW + timedelta(days=15)
    assert env.rules.judge(goal, plays, end, env.pack).verdict == "failed"
    plays.append(_event("play_started", NOW + timedelta(days=5), title_id="T0001"))
    verdict = env.rules.judge(goal, plays, end, env.pack)
    assert verdict.verdict == "succeeded" and verdict.success_at == iso(NOW + timedelta(days=5))


def test_resume_goal_only_counts_the_same_series(env):
    goal = _goal("G07", slug="hapi-d1")
    end = NOW + timedelta(days=4)
    other = _event("play_started", NOW + timedelta(hours=1), title_id="T0005", episode_id="tidha-d1-s1e2")
    same = _event("play_started", NOW + timedelta(hours=2), title_id="T0004", episode_id="hapi-d1-s1e2")
    assert env.rules.judge(goal, [other], end, env.pack).verdict == "failed"
    assert env.rules.judge(goal, [other, same], end, env.pack).success_event_id == same["event_id"]
