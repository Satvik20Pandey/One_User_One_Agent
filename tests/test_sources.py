"""Conflicting and duplicated sources: precedence is per fact and conflicts stay visible."""
from helpers import make_trigger


def test_three_way_dialect_conflict_uses_app_setting(env):
    # app says d1, profile says d3, the campaign link says d2
    trigger = make_trigger("U-C1", dialect="d1", deeplink="ott://c/cmp1/d2/title/T0101")
    record, actions = env.decide(trigger)

    assert record["dialect"]["used"] == "d1" and record["dialect"]["source"] == "app_setting"
    assert set(record["dialect"]["conflicts"]) == {"profile:d3", "deeplink:d2"}
    assert any(c.startswith("dialect:") for c in record["conflicts"])

    page = next(a for a in actions if a["type"] == "home_page")
    assert page["dialect"] == "d1"
    assert all(env.pack.title(t).dialect == "d1" for row in page["rows"] for t in row["title_ids"])


def test_duplicate_profile_rows_newest_wins(env):
    facts = env.facts_for("U-D1")
    assert facts.value("subscription_status") == "lapsed"
    assert facts.value("consent_push") is True  # from the newer row
    assert any("2 rows" in c for c in facts.conflicts)


def test_purchase_event_after_snapshot_overrides_status(env):
    facts = env.facts_for("U-P1")
    status = facts.facts["subscription_status"]
    assert status.value == "active" and status.source == "event"
    assert status.evidence == ["event:e-P1-1"]
    assert any(c.startswith("subscription_status: profile=never") for c in facts.conflicts)
    assert env.rules.check("G05", facts).eligible is False


def test_opt_out_event_overrides_snapshot_consent(env):
    facts = env.facts_for("U-O1")
    assert facts.facts["consent_push"].value is False
    assert facts.facts["consent_push"].source == "event"

    _, actions = env.decide(make_trigger("U-O1"))
    assert all(a["type"] != "push" for a in actions)


def test_duplicate_history_events_are_stored_once(env):
    assert env.stats["counts"]["duplicates_dropped"] == 1
    purchases = [e for e in env.store.events_for("U-P1") if e["event"] == "purchase_completed"]
    assert len(purchases) == 1


def test_open_without_a_dialect_uses_the_apps_latest_setting_not_the_profile(env):
    # U-C1's profile says d3, its earlier opens were in d1; this open carries no dialect at all
    record, actions = env.decide(make_trigger("U-C1", dialect="NA"))
    assert record["dialect"]["used"] == "d1" and record["dialect"]["source"] == "app_setting_latest"
    assert "profile:d3" in record["dialect"]["conflicts"] and "app_setting:missing on this open" in record["dialect"]["conflicts"]
    assert all(a["dialect"] == "d1" for a in actions)
    assert not any(a["type"] in ("push", "whatsapp") for a in actions)  # disagreeing evidence: page only


def test_message_in_a_dialect_the_app_is_not_set_to_is_vetoed(env):
    from viewer_agent.agent import Budget
    from viewer_agent.content import MessageDraft
    from viewer_agent.planner import Proposal
    from viewer_agent.times import next_send_slot
    from viewer_agent.verifier import verify
    from helpers import NOW
    facts = env.facts_for("U-C1", make_trigger("U-C1", dialect="NA"))
    msg = MessageDraft("push", "d3", "T0201", next_send_slot(NOW, 19), text={"title": "Only", "body": "Watch Only now."},
                       deeplink="ott://title/T0201")
    result = verify(Proposal(env.rules.check("G02", facts) if env.rules.check("G02", facts).eligible else None, None, msg, None),
                    facts, "agent", env.rules, env.store, env.pack, env.settings, NOW, Budget(NOW, env.settings))
    assert any(f["check"] == "message_dialect" and "last seen set to d1" in f["reason"] for f in result.failed)
