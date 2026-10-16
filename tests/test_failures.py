"""Tool and model failures: the agent degrades to a smaller safe action or no action, inside the time budget."""
from datetime import timedelta

from helpers import NOW, make_env, make_trigger
from viewer_agent.config import Settings


def test_model_outage_falls_back_to_rules(env):
    env.provider.set_outage(NOW - timedelta(minutes=1), NOW + timedelta(minutes=5))
    record, actions = env.decide(make_trigger("U-S1"))
    assert record["status"] == "PUBLISHED" and actions
    assert record["fallback_used"] == "model_unavailable"
    assert "503" in record["model"]["failure"]
    assert record["decided_in_ms"] < 60_000


def test_model_timeout_is_bounded_by_our_own_deadline(env):
    record, actions = env.decide(make_trigger("U-A2", event_id="trg-A2-timeout"))  # scripted 45 s timeout
    assert record["status"] == "PUBLISHED" and actions
    assert record["fallback_used"] == "model_unavailable" and "timeout" in record["model"]["failure"]
    assert record["budget"]["simulated_wait_ms"] == 10_000  # we waited our 10 s, not the model's 45 s
    assert 10_000 <= record["decided_in_ms"] < 60_000


def test_malformed_model_output_falls_back(env):
    record, actions = env.decide(make_trigger("U-A2", event_id="trg-A2-malformed"))
    assert record["status"] == "PUBLISHED" and actions
    assert "JSONDecodeError" in record["model"]["failure"]


def test_store_unavailable_degrades_to_a_dialect_only_page(env):
    env.store.degrade(NOW + timedelta(minutes=10), timeout_s=8.0, timeout_rate=1.0, slow_s=2.0)
    record, actions = env.decide(make_trigger("U-S1"))

    assert record["fallback_used"] == "store_degraded"
    assert record["goal"] is None and "user_data_store" in record["no_goal_reason"]
    assert record["status"] == "PUBLISHED" and [a["type"] for a in actions] == ["home_page"]
    assert actions[0]["dialect"] == "d1"
    assert record["decided_in_ms"] <= 60_000

    env.store.degrade(None)
    assert env.store.load_state("U-S1") is None  # nothing was overwritten while the agent was blind


def test_store_unavailable_with_unknown_dialect_means_no_action(env):
    env.store.degrade(NOW + timedelta(minutes=10), timeout_s=8.0, timeout_rate=1.0, slow_s=2.0)
    record, actions = env.decide(make_trigger("U-S1", dialect="NA"))
    assert record["status"] == "NO_ACTION" and actions == []
    assert "nothing safe" in record["no_action_reason"]


def test_tool_budget_overrun_is_a_recorded_no_action(tmp_path):
    env = make_env(tmp_path, Settings(max_tool_calls=6))
    record, actions = env.decide(make_trigger("U-S1"))
    assert record["status"] == "NO_ACTION" and actions == []
    assert record["fallback_used"] == "budget_exceeded"
    assert "budget" in record["no_action_reason"]
