"""Copy must be true when it is sent, closed goals take their messages with them, and thin evidence means no action."""
import json
from datetime import timedelta

from helpers import NOW, make_event, make_trigger
from viewer_agent.agent import ViewerAgent
from viewer_agent.content import build_message
from viewer_agent.times import iso, parse_ts


def test_new_release_copy_says_how_new(env):
    facts = env.facts_for("U-S1")
    msg = build_message("G08", facts, env.pack, NOW, env.settings)
    assert msg.channel == "whatsapp"
    env.pack.profiles["U-S1"]["whatsapp_opt_in"] = False
    push = build_message("G08", env.facts_for("U-S1"), env.pack, NOW, env.settings)
    assert "came out" in push.text["body"] and "just released" not in push.text["body"]


def test_trial_ending_message_only_when_the_trial_really_ends_soon(env):
    # U-T1's trial ends 2026-10-01 08:05 UTC; the next 19:00 IST slot is after that, so "ending soon" would be false
    facts = env.facts_for("U-T1")
    assert facts.value("trial_ends_at") == "2026-10-01T08:05:00.000Z"
    assert build_message("G04", facts, env.pack, NOW, env.settings) is None

    # a trial bought this morning ends in three days: now the message is true
    env.store.add_event(make_event("U-T1", "purchase_completed", NOW - timedelta(hours=1),
                                   {"plan_id": "P_TRIAL", "price_inr": 1, "is_trial": True}))
    facts = env.facts_for("U-T1")
    assert parse_ts(facts.value("trial_ends_at")) == NOW - timedelta(hours=1) + timedelta(days=3)
    msg = build_message("G04", facts, env.pack, NOW, env.settings)
    assert msg is not None and msg.template_id == "wa_trial_ending_v1"


def test_a_purchase_cancels_the_upsell_it_made_moot(env):
    profile = env.pack.profiles["U-T1"]
    profile["plan_ends_at"] = iso(NOW + timedelta(days=2))  # a trial that ends in two days: the agent will message
    # a viewer who has already played, so "first play" no longer comes before "convert the trial"
    env.store.add_event(make_event("U-T1", "play_started", NOW - timedelta(days=1), {"title_id": "T0001", "episode_id": "rava-d1-s1e1"}))
    record, actions = env.decide(make_trigger("U-T1"))
    assert record["goal"]["id"] == "G04" and any(a["type"] == "whatsapp" for a in actions)

    paid_at = NOW + timedelta(minutes=2)
    note = ViewerAgent("U-T1", env.deps).handle_event(
        make_event("U-T1", "purchase_completed", paid_at, {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False}), paid_at)
    assert "cancelled scheduled message" in note["note"]
    message = next(a for a in env.store.actions_for("U-T1") if a["type"] == "whatsapp")
    assert message["cancelled_at"] and "G04 closed" in message["cancel_reason"]


def test_buffered_open_gets_no_action_even_when_the_store_is_down(env):
    env.store.degrade(NOW + timedelta(minutes=10), timeout_s=8.0, timeout_rate=1.0, slow_s=2.0)
    record, actions = env.decide(make_trigger("U-S1", occurred_at=iso(NOW - timedelta(days=2))))
    assert record["status"] == "NO_ACTION" and actions == [] and "buffered offline" in record["no_action_reason"]


def test_eligibility_assumptions_are_on_the_decision(env):
    record, _ = env.decide(make_trigger("U-R1"))  # free viewer: G07 here, but G02's "no completion yet" style notes
    env.pack.profiles["U-D1"]["notifications_opt_in"] = True
    lapsed, _ = env.decide(make_trigger("U-D1"))
    assert lapsed["goal"]["id"] == "G06"
    assert any("recently lapsed" in a for a in lapsed["assumptions"])


def test_not_enough_evidence_is_a_recorded_decision(env):
    # no profile row, and the app and the campaign link disagree on dialect: too thin to act on
    env.store.add_event(make_event("U-B1", "signup_completed", NOW - timedelta(minutes=5), {"method": "otp"}))
    record, actions = env.decide(make_trigger("U-B1", deeplink="ott://c/cmp1/d2/title/T0101"))
    assert record["status"] == "NO_ACTION" and actions == []
    assert "not enough evidence" in record["no_action_reason"]
    assert record["goal"]["id"] == "G01"  # the goal is still recorded, so the holdout comparison stays fair


def test_g03_correction_only_applies_to_goals_still_open(env):
    correction = {"at": iso(NOW + timedelta(days=1)), "goal_id": "G03",
                  "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"}}
    env.rules.apply_correction(correction)
    ended_before = {"goal_id": "G03", "started_at": iso(NOW - timedelta(hours=60)), "deadline": iso(NOW - timedelta(hours=12))}
    still_open = {"goal_id": "G03", "started_at": iso(NOW), "deadline": iso(NOW + timedelta(hours=48))}
    assert env.rules.success_for(ended_before)[0]["event"] == "app_open"
    assert env.rules.success_for(still_open)[0]["event"] == "play_started"
    assert "+G03@" in env.rules.success_for(still_open)[1] and "+G03@" not in env.rules.success_for(ended_before)[1]


def test_mock_model_really_advises_and_its_advice_is_bounded(env):
    record, actions = env.decide(make_trigger("U-A1"))  # G02: the hero is a popularity pick, so the model is asked
    page = next(a for a in actions if a["type"] == "home_page")
    assert record["model"]["used"] and record["model"]["changed"] == f"hero -> {page['hero_title_id']}"
    assert page["hero_title_id"] not in [t for r in page["rows"] for t in r["title_ids"]]
    assert all(len(r["title_ids"]) >= 3 for r in page["rows"])
    assert "top genre" in record["model"]["reason"]
    json.dumps(record)  # the record stays machine-readable


def test_model_hero_never_duplicates_content_in_another_language(env):
    # "Raloja" exists in Hindi (T0003) and English (T0008); a hero swap must remove both from the rows
    from viewer_agent.planner import Advice, Proposal, _ask_model
    from viewer_agent.agent import Budget
    from viewer_agent.content import PageDraft, Row
    facts = env.facts_for("U-A1")
    page = PageDraft("d1", "T0001", [Row("popular_in_dialect", ["T0008", "T0004", "T0005", "T0009"])],
                     candidate_ids=["T0001", "T0003", "T0004", "T0005", "T0009"])

    class PicksRaloja:
        name = "fake"

        def advise(self, request):
            return Advice(goal_id="G02", hero_title_id="T0003", reason="test")

    proposal = Proposal(env.rules.check("G02", facts), page, None, None)
    _ask_model(proposal, facts, env.rules.eligible(facts), env.pack, env.settings, NOW, PicksRaloja(), Budget(NOW, env.settings))
    assert proposal.page.hero_title_id == "T0003"
    assert "T0008" not in proposal.page.rows[0].title_ids


def test_model_never_replaces_a_hero_the_evidence_chose(env):
    # push-only viewer with a resume goal: the model is asked for push copy, and must not move the series off the page
    from viewer_agent.planner import Advice

    class PushesAnotherTitle:
        name = "fake"

        def advise(self, request):
            return Advice(goal_id="G07", hero_title_id="T0009", reason="prefers a film")

    env.deps.provider = PushesAnotherTitle()
    env.pack.profiles["U-R1"]["whatsapp_opt_in"] = False
    record, actions = env.decide(make_trigger("U-R1"))
    page = next(a for a in actions if a["type"] == "home_page")
    push = next(a for a in actions if a["type"] == "push")
    assert record["model"]["calls"] == 1 and page["hero_title_id"] == "T0004" == push["title_id"]
    assert "kept the goal series hero" in record["model"]["note"]


def test_model_copy_may_reword_but_not_add_facts():
    from viewer_agent.planner import _copy_adds_facts
    rules = {"title": "Continue Hapi", "body": "Episode 2 of Hapi is waiting for you."}
    assert _copy_adds_facts({"title": "Hapi is calling", "body": "Your episode 2 awaits."}, rules, "Hapi") is None
    assert "numbers" in _copy_adds_facts({"title": "Hapi", "body": "Episode 5 now, only Rs 49!"}, rules, "Hapi")
    assert "name the title" in _copy_adds_facts({"title": "Something new", "body": "Watch now."}, rules, "Hapi")


def test_new_release_goal_ignores_titles_released_after_the_goal_started(env):
    goal = {"goal_id": "G08", "started_at": iso(parse_ts("2026-09-01T00:00:00.000Z")),
            "deadline": iso(parse_ts("2026-10-15T00:00:00.000Z")), "context": {}}
    play = lambda tid: {"event": "play_started", "event_id": tid, "_at": parse_ts("2026-10-01T00:00:00.000Z"),  # noqa: E731
                        "properties": {"title_id": tid}}
    assert env.rules._matches(goal, play("T0014"), env.pack) is False   # released 2025: too old
    assert env.rules._matches(goal, play("T0006"), env.pack) is False   # released 2026-09-20: after the goal began
    goal["started_at"] = iso(parse_ts("2026-09-25T00:00:00.000Z"))
    assert env.rules._matches(goal, play("T0006"), env.pack) is True    # 5 days before the goal began


def test_a_stale_write_is_retried_on_fresh_state(env, monkeypatch):
    from viewer_agent.store import StaleState
    real, calls = env.store.save_state, []

    def flaky(user_id, state, now):
        calls.append(user_id)
        if len(calls) == 1:
            raise StaleState("another worker saved first")
        return real(user_id, state, now)

    monkeypatch.setattr(env.store, "save_state", flaky)
    record, actions = env.decide(make_trigger("U-R1"))
    assert len(calls) == 2 and record["status"] == "PUBLISHED" and actions
    assert any("decided again" in a for a in record["assumptions"])
    assert len(env.store.decisions_for("U-R1")) == 1  # the abandoned first attempt wrote nothing


def test_page_must_still_be_valid_when_it_goes_live(env):
    from viewer_agent.agent import Budget
    from viewer_agent.content import PageDraft, Row
    from viewer_agent.planner import Proposal
    from viewer_agent.verifier import verify
    facts = env.facts_for("U-A1")
    env.pack.withdraw("T0004", NOW + timedelta(seconds=30))  # withdrawn 30 s after the open arrived
    page = PageDraft("d1", "T0001", [Row("popular_in_dialect", ["T0004", "T0005", "T0009"])])
    result = verify(Proposal(env.rules.check("G02", facts), page, None, None), facts, "agent", env.rules, env.store,
                    env.pack, env.settings, NOW, Budget(NOW, env.settings))
    assert any(f["check"] == "availability" for f in result.failed)
