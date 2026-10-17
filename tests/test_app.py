"""The Streamlit app, driven headlessly: it renders and every scenario runs the real agent."""
import json
from datetime import timedelta
from pathlib import Path

import pytest

from helpers import MINI, NOW, read_jsonl
from viewer_agent.judge import run_judge, write_reports
from viewer_agent.replay import run_replay, write_jsonl
from viewer_agent.times import iso

try:  # the app needs streamlit >= 1.50; skip cleanly where an older or broken install is present
    from streamlit.testing.v1 import AppTest
    import altair  # noqa: F401
except Exception:  # pragma: no cover
    pytest.skip("streamlit is not usable in this environment", allow_module_level=True)

APP = Path(__file__).parents[1] / "app.py"


@pytest.fixture(scope="module")
def mini_run(tmp_path_factory):
    out = tmp_path_factory.mktemp("run")
    run_replay(MINI, out)
    (out / "sim").mkdir()
    page = next(a for a in read_jsonl(out / "actions.jsonl") if a["user_id"] == "U-A1")
    write_jsonl(out / "sim" / "outcome_events.jsonl", [
        {"event_id": "sim-1", "user_id": "U-A1", "event": "home_page_shown", "occurred_at": iso(NOW + timedelta(seconds=40)),
         "sent_at": iso(NOW + timedelta(seconds=40)), "received_at": iso(NOW + timedelta(seconds=40)), "session_id": "sim",
         "app_dialect": "d1", "app_language": None, "platform": None, "app_version": None,
         "properties": {"personalised": True, "page_action_id": page["action_id"]}}])
    (out / "sim" / "simulator_report.json").write_text(
        json.dumps({"seed": "test", "events_written": 1, "counts": {}, "policy_problem_count": 0}))
    run_judge(MINI, out)
    write_reports(MINI, out)
    return out


@pytest.fixture
def app(mini_run, monkeypatch):
    monkeypatch.setenv("VIEWER_AGENT_RUN", str(mini_run))
    monkeypatch.setenv("VIEWER_AGENT_PACK", str(MINI))
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    return at


def _loop(at, viewer, scenario="Normal"):
    at.selectbox(key="viewer").set_value(viewer)
    at.selectbox(key="scenario").set_value(scenario)
    at.run()
    assert not at.exception, at.exception
    return at.session_state["last_result"]


def test_page_renders_with_headline_numbers(app):
    labels = [m.label for m in app.metric]
    assert labels == ["App opens decided", "On time (≤ 60 s)", "Acted / chose not to", "Policy violations"]
    assert [s.value for s in app.subheader] == ["Run the loop for one viewer", "Did the agent help?"]


def test_normal_run_matches_the_recorded_decision(app, mini_run):
    result = _loop(app, "U-S1")
    recorded = next(d for d in read_jsonl(mini_run / "decisions.jsonl") if d["viewer_id"] == "U-S1")
    assert result["decision"]["status"] == recorded["status"] == "PUBLISHED"
    assert result["decision"]["goal"]["id"] == recorded["goal"]["id"]


def test_every_failure_scenario(app):
    assert "G04" in _loop(app, "U-S1", "Model gives a false answer")["decision"]["model"]["rejected"]
    assert _loop(app, "U-S1", "Model is down")["decision"]["fallback_used"] == "model_unavailable"
    blind = _loop(app, "U-S1", "Data store is down")["decision"]
    assert blind["fallback_used"] == "store_degraded" and blind["goal"] is None
    assert _loop(app, "U-S1", "Same open arrives twice")["second"]["status"] == "DUPLICATE"
    late = _loop(app, "U-S1", "Open arrives 2 days late")["decision"]
    assert late["status"] == "NO_ACTION" and "buffered offline" in late["no_action_reason"]


def test_holdout_viewer_never_gets_an_action(app):
    result = _loop(app, "U-H1")
    assert result["decision"]["status"] == "HOLDOUT_NO_ACTION" and result["actions"] == []
