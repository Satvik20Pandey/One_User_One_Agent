"""Goal rules: corrections, measurability, assumptions and evidence."""
from datetime import timedelta

from helpers import MINI, NOW, make_event, make_trigger, read_jsonl
from viewer_agent.agent import ViewerAgent
from viewer_agent.config import Settings
from viewer_agent.goals import RuleBook
from viewer_agent.replay import Replay
from viewer_agent.times import iso

CORRECTION = next(c for c in read_jsonl(MINI / "replay" / "control_events.jsonl") if c["type"] == "goal_rule_corrected")


def _goal(goal_id, hours=48, **context):
    return {"goal_id": goal_id, "started_at": iso(NOW), "deadline": iso(NOW + timedelta(hours=hours)), "context": context}


def _event(event, at, **props):
    return {"event": event, "_at": at, "event_id": f"{event}@{at.isoformat()}", "properties": props}


def test_g03_correction_changes_how_open_goals_are_judged(env):
    goal = _goal("G03")
    next_day = NOW + timedelta(days=1)
    after = NOW + timedelta(days=3)
    open_only = [_event("app_open", next_day)]
    assert env.rules.judge(goal, open_only, after, env.pack).verdict == "succeeded"

    env.rules.apply_correction(CORRECTION)
    assert env.rules.version.endswith("+G03@2026-10-01T05:06:00.000Z")
    assert env.rules.judge(goal, open_only, after, env.pack).verdict == "failed"
    with_play = open_only + [_event("play_started", next_day, title_id="T0001")]
    assert env.rules.judge(goal, with_play, after, env.pack).verdict == "succeeded"


def test_correction_touches_only_viewers_with_an_open_g03_goal(tmp_path):
    replay = Replay(MINI, tmp_path / "out")
    for user_id, goal_id in (("U-A1", "G03"), ("U-S1", "G07")):
        replay.store.save_state(user_id, {"user_id": user_id, "goal_history": [], "learnings": [],
                                          "active_goal": {"goal_id": goal_id, "rule_version": replay.rules.version}}, NOW)
    log = replay.handle_control(CORRECTION, NOW)
    assert log["open_goals_affected"] == 1 and log["viewers"] == ["U-A1"]
    assert "corrected" in replay.store.load_state("U-A1")["learnings"][0]
    assert replay.store.load_state("U-S1")["learnings"] == []
    replay.store.close()


def test_unmeasurable_goals_are_never_offered(env):
    for user_id in ("U-A1", "U-S1", "U-H1"):
        offered = [e.goal_id for e in env.rules.eligible(env.facts_for(user_id))]
        assert "G10" not in offered and "G11" not in offered
    assert env.rules.measurable("G10") == (False, "success condition names no event")
    assert env.rules.measurable("G11")[1].startswith("success event 'tv_app_open' never appears")


def test_recently_lapsed_window_is_an_explicit_assumption(env):
    facts = env.facts_for("U-D1")  # lapsed 2026-09-25
    lenient = env.rules.check("G06", facts)
    assert lenient.eligible and any("recently lapsed" in a for a in lenient.assumptions)
    strict = RuleBook(env.pack.goals_config, Settings(g06_recent_days=3), known_events=env.stats["event_names"])
    assert strict.check("G06", facts).eligible is False


def test_eligibility_carries_evidence_refs(env):
    facts = env.facts_for("U-S1")
    resume = env.rules.check("G07", facts)
    assert resume.eligible and resume.evidence == ["event:e-S1-2"]
    assert resume.context == {"slug": "hapi-d1", "title_id": "T0004"}


def test_viewer_without_evidence_gets_no_goal_until_the_signup_arrives(env):
    record, actions = env.decide(make_trigger("U-B1"))  # no profile row, no events
    assert record["status"] == "NO_ACTION" and record["goal"] is None
    assert "no evidence" in record["no_action_reason"]

    signup_at = NOW + timedelta(seconds=20)
    ViewerAgent("U-B1", env.deps).handle_event(make_event("U-B1", "signup_completed", signup_at, {"method": "otp"}), signup_at)

    later = NOW + timedelta(minutes=1)
    record, actions = env.decide(make_trigger("U-B1", at=later), later)
    assert record["status"] == "PUBLISHED" and record["goal"]["id"] == "G01"
    assert any("inferred from signup_completed" in a for a in record["assumptions"])


def test_resume_content_promotes_the_series_the_goal_is_about(env):
    # U-R1 left "hapi" untouched since 09-20 (the goal) but started "tidha" yesterday (more recent, not the goal)
    env.store.add_event(make_event("U-R1", "play_started", NOW - timedelta(days=1),
                                   {"title_id": "T0005", "episode_id": "tidha-d1-s1e1"}))
    record, actions = env.decide(make_trigger("U-R1"))
    assert record["goal"]["id"] == "G07" and "hapi-d1" in record["eligibility"]["reason"]

    page = next(a for a in actions if a["type"] == "home_page")
    message = next(a for a in actions if a["type"] == "whatsapp")
    assert page["hero_title_id"] == "T0004"
    assert page["rows"][0]["row_type"] == "next_episode" and page["rows"][0]["title_ids"][0] == "T0005"
    assert message["title_id"] == "T0004" and message["variables"]["title_name"] == "Hapi"
