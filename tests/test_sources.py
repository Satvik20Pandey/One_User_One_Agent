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
