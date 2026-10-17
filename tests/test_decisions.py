"""How the agent chooses: lifecycle-stage goals, evidence-gated messages, in-app fallback, when to ask the model,
and what it learned from earlier goals."""
from datetime import timedelta

from helpers import NOW, make_trigger


def test_goal_follows_the_viewer_lifecycle_stage(env):
    # an active subscriber who rarely plays gets the habit goal, even though a series is unfinished
    subscriber, _ = env.decide(make_trigger("U-S1"))
    assert subscriber["goal"]["id"] == "G09"
    # a free viewer with an untouched series gets the resume goal
    free, _ = env.decide(make_trigger("U-R1"))
    assert free["goal"]["id"] == "G07"


def test_messages_need_stronger_evidence_than_pages(env):
    # same viewer, but the campaign link names another dialect: the evidence now disagrees
    record, actions = env.decide(make_trigger("U-R1", deeplink="ott://c/cmp9/d2/title/T0101"))
    assert [a["type"] for a in actions] == ["home_page"]
    assert any("below the 0.6 a push or WhatsApp needs" in a for a in record["assumptions"])


def test_viewer_without_message_consent_gets_an_in_app_message_once_a_day(env):
    profile = env.pack.profiles["U-R1"]
    profile["notifications_opt_in"] = profile["whatsapp_opt_in"] = False
    record, actions = env.decide(make_trigger("U-R1"))
    in_app = next(a for a in actions if a["type"] == "in_app_message")
    assert in_app["dialect"] == "d1" and in_app["text"]["title"].startswith("Continue")
    assert "consent" not in record["checks"]["passed"]  # no consent is needed inside the app

    later = NOW + timedelta(hours=3)  # the page has expired, but today's in-app message is already out
    record, actions = env.decide(make_trigger("U-R1", at=later), later)
    assert [a["type"] for a in actions] == ["home_page"]
    assert any("in-app message was already shown today" in a for a in record["assumptions"])


def test_model_is_asked_only_when_it_can_add_something(env):
    # resume goal: the hero is the goal's series and WhatsApp copy is an approved template -> nothing to ask
    resume, _ = env.decide(make_trigger("U-R1"))
    assert resume["model"]["calls"] == 0 and "not needed" in resume["model"]["skipped"]
    # first full episode: the hero is a popularity pick, which the model may improve
    generic, _ = env.decide(make_trigger("U-A2", event_id="trg-A2-generic"))
    assert generic["model"]["calls"] == 1


def test_a_goal_that_failed_is_not_retried_straight_away(env):
    env.decide(make_trigger("U-R1"))
    state = env.store.load_state("U-R1")
    failed = dict(state["active_goal"], deadline=(NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                  outcome={"verdict": "failed", "attribution": "exposed_no_success"})
    state["goal_history"], state["active_goal"] = [failed], None
    env.store.save_state("U-R1", state, NOW)

    later = NOW + timedelta(hours=3)
    record, _ = env.decide(make_trigger("U-R1", at=later), later)
    assert (record.get("goal") or {}).get("id") != "G07"
    assert any(a.startswith("learned: G07 failed") for a in record["assumptions"])


def test_evidence_refs_carry_their_own_time_and_age(env):
    record, _ = env.decide(make_trigger("U-R1"))
    play = next(e for e in record["evidence"] if e["ref"] == "event:e-R1-2")
    assert play["observed_at"].startswith("2026-09-20T15:01") and play["age_s"] > 10 * 86400
    assert record["facts"]["subscription_status"]["value"] == "never"  # the full fact table is on the record


def test_a_resume_goal_gives_way_when_its_series_cannot_be_shown(env):
    # U-R1 qualifies for G07 (resume "Hapi") and G05 (first paid plan). When Hapi is held for review, the agent
    # cannot show it, so it pursues G05 instead; when Hapi is showable, G07 comes first.
    record, _ = env.decide(make_trigger("U-R1"))
    assert record["goal"]["id"] == "G07"

    env.pack.flagged_text["T0004"] = "held for review"
    other, _ = env.decide(make_trigger("U-R1", at=NOW + timedelta(days=4)), NOW + timedelta(days=4))
    assert other["goal"]["id"] == "G05"
    assert any("passed over G07" in a for a in other["assumptions"])
