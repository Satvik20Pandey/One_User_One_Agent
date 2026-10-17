"""The Streamlit app, driven headlessly: every tab renders and "Try the agent" returns real agent decisions."""
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


def _run_agent(at, viewer, **switches):
    at.selectbox(key="try_viewer").set_value(viewer)
    for name, on in switches.items():
        at.checkbox(key=f"try_{name}").set_value(on)
    at.button(key="try_run").click().run()
    assert not at.exception, at.exception
    return at.session_state["try_result"]


def test_every_tab_renders(app):
    assert [t.label for t in app.tabs] == ["Overview", "Explore a viewer", "Try the agent", "Impact vs holdout"]
    labels = [m.label for m in app.metric]
    assert "Policy violations" in labels and "Difference" in labels
    assert any("U-" in str(s.value) for s in app.selectbox) or app.text_input


def test_try_the_agent_runs_the_real_agent(app):
    result = _run_agent(app, "U-S1")
    assert result["decision"]["status"] == "PUBLISHED"
    assert result["decision"]["goal"]["id"] == "G07"
    assert {a["type"] for a in result["actions"]} >= {"home_page"}


def test_try_the_agent_with_failures(app):
    claimed = _run_agent(app, "U-S1", claim=True)["decision"]
    assert "G04" in claimed["model"]["rejected"]

    down = _run_agent(app, "U-S1", claim=False, outage=True)["decision"]
    assert down["fallback_used"] == "model_unavailable" and down["status"] == "PUBLISHED"

    blind = _run_agent(app, "U-S1", outage=False, store=True)["decision"]
    assert blind["fallback_used"] == "store_degraded" and blind["goal"] is None

    twice = _run_agent(app, "U-S1", store=False, twice=True)
    assert twice["duplicate"]["status"] == "DUPLICATE"


def test_holdout_viewer_never_gets_an_action(app):
    result = _run_agent(app, "U-H1")
    assert result["decision"]["status"] == "HOLDOUT_NO_ACTION" and result["actions"] == []
