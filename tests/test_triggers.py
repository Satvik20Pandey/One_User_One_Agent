"""Late, duplicate and repeated triggers."""
from datetime import timedelta

from helpers import NOW, make_trigger
from viewer_agent.times import iso


def test_duplicate_trigger_is_idempotent(env):
    trigger = make_trigger("U-A1")
    first, actions = env.decide(trigger)
    assert first["status"] == "PUBLISHED" and actions

    again, actions_again = env.decide(trigger)
    assert again["status"] == "DUPLICATE"
    assert again["duplicate_of"] == first["decision_id"]
    assert actions_again == []
    assert len(env.store.actions_for("U-A1")) == len(actions)


def test_buffered_trigger_is_recorded_but_not_acted_on(env):
    # occurred two days ago on the phone, sent and received just now
    trigger = make_trigger("U-A1", occurred_at=iso(NOW - timedelta(days=2)))
    record, actions = env.decide(trigger)

    assert record["status"] == "NO_ACTION" and actions == []
    assert "buffered offline" in record["no_action_reason"]
    assert record["trigger"]["live_visit"] is False
    assert record["trigger"]["event_age_s"] >= 2 * 86400 - 5

    state = env.store.load_state("U-A1")
    assert state["facts"]["trigger_freshness"]["value"]["live_visit"] is False
    assert state["active_goal"] is None  # no goal window opens on a visit that did not happen


def test_second_open_within_page_ttl_changes_nothing(env):
    first, _ = env.decide(make_trigger("U-A1"))

    soon = NOW + timedelta(minutes=30)
    record, actions = env.decide(make_trigger("U-A1", at=soon), soon)
    assert record["status"] == "NO_ACTION" and actions == []
    assert "still live" in record["no_action_reason"]
    assert record["goal"]["id"] == first["goal"]["id"] and record["goal"]["committed_now"] is False

    later = NOW + timedelta(hours=3)  # page TTL is 120 minutes
    record, actions = env.decide(make_trigger("U-A1", at=later), later)
    assert record["status"] == "PUBLISHED" and actions


def test_device_clock_ahead_is_treated_as_live(env):
    trigger = make_trigger("U-A1", occurred_at=iso(NOW + timedelta(hours=2)))
    record, _ = env.decide(trigger)
    assert record["trigger"]["live_visit"] is True
    assert record["trigger"]["device_clock_trusted"] is False
    assert any("clock" in a for a in record["assumptions"])
