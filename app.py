"""Streamlit demo: the decide, act and measure loop for one viewer.

    streamlit run app.py

Pick a viewer and, if you like, something that goes wrong. The real agent runs live on that viewer's app open
(mock model, in-memory copy of the data, so the saved run never changes). The "Measure" step shows what that viewer
actually did afterwards in the sample run. Paths: VIEWER_AGENT_RUN (default sample_run/), VIEWER_AGENT_PACK (./pack).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))  # so the app also runs without `pip install -e .`

from viewer_agent.agent import Deps, ViewerAgent  # noqa: E402
from viewer_agent.config import Settings  # noqa: E402
from viewer_agent.goals import RuleBook  # noqa: E402
from viewer_agent.judge import _diff_ci, _pp, _rate  # noqa: E402
from viewer_agent.pack import Pack  # noqa: E402
from viewer_agent.planner import MockProvider  # noqa: E402
from viewer_agent.replay import read_jsonl  # noqa: E402
from viewer_agent.store import Store  # noqa: E402
from viewer_agent.times import IST, iso, parse_ts  # noqa: E402

RUN = Path(os.environ.get("VIEWER_AGENT_RUN", ROOT / "sample_run"))
PACK = Path(os.environ.get("VIEWER_AGENT_PACK", ROOT / "pack"))
AGENT, HOLDOUT = "#2a78d6", "#eb6834"
SCENARIOS = ["Normal", "Model gives a false answer", "Model is down", "Data store is down",
             "Same open arrives twice", "Open arrives 2 days late"]

st.set_page_config(page_title="One User, One Agent", page_icon="🎬", layout="centered")
st.markdown("""<style>
  .block-container { padding-top: 2rem; max-width: 980px; }
  [data-testid="stMetricValue"] { font-size: 1.45rem; }
  .step { font-size: .78rem; letter-spacing: .06em; text-transform: uppercase; color: #6b7280; margin-bottom: .2rem; }
  .big { font-size: 1.12rem; font-weight: 600; line-height: 1.35; margin-bottom: .35rem; }
</style>""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_run(run_dir: str) -> dict:
    run = Path(run_dir)
    decisions = read_jsonl(run / "decisions.jsonl")
    by_viewer = defaultdict(list)
    for d in decisions:
        by_viewer[d["viewer_id"]].append(d)
    outcomes = defaultdict(list)
    for o in read_jsonl(run / "outcomes.jsonl"):
        outcomes[o["user_id"]].append(o)
    return {"summary": json.loads((run / "replay_summary.json").read_text(encoding="utf-8")),
            "latency": json.loads((run / "latency_report.json").read_text(encoding="utf-8")),
            "sim": json.loads((run / "sim" / "simulator_report.json").read_text(encoding="utf-8")),
            "by_viewer": dict(by_viewer), "outcomes": dict(outcomes)}


@st.cache_resource(show_spinner="Loading the data pack and 30 days of history…")
def load_engine(pack_dir: str) -> dict:
    """Loaded once. Two copies of the catalogue: before and after the mid-day title withdrawal."""
    pack_dir = Path(pack_dir)
    store = Store(":memory:", shared=True)  # read-only after this; every run works on its own snapshot
    stats = store.ingest_history(pack_dir / "data" / "events.jsonl")
    controls = read_jsonl(pack_dir / "replay" / "control_events.jsonl")
    before, after = Pack(pack_dir), Pack(pack_dir)
    for p in (before, after):
        p.popularity = stats["plays_per_title"]
    for c in controls:
        if c["type"] == "title_withdrawn":
            after.withdraw(c["title_id"], parse_ts(c["at"]))
    first_trigger, live = {}, defaultdict(list)
    for t in read_jsonl(pack_dir / "replay" / "triggers.jsonl"):
        first_trigger.setdefault(t["user_id"], t)
    for e in read_jsonl(pack_dir / "replay" / "live_events.jsonl"):
        live[e["user_id"]].append(e)
    return {"store": store, "known_events": stats["event_names"], "controls": controls, "live": dict(live),
            "pack_before": before, "pack_after": after, "first_trigger": first_trigger}


def examples(run: dict) -> dict[str, str]:
    """A few real viewers whose first app open of the day shows a typical situation."""
    revised = {r["user_id"] for r in run["summary"]["plan_revisions_from_live_events"]}
    seen = lambda u: any(o["attribution"] == "exposed_before_success" for o in run["outcomes"].get(u, []))  # noqa: E731
    tests = [
        ("Agent acted, goal reached", lambda u, d: d["status"] == "PUBLISHED" and len(d["actions"]) >= 2 and seen(u)),
        ("New viewer, first play", lambda u, d: d["status"] == "PUBLISHED" and (d.get("goal") or {}).get("id") == "G01"),
        ("Holdout viewer (control group)", lambda u, d: d["status"] == "HOLDOUT_NO_ACTION" and d.get("goal")),
        ("Three dialects disagree", lambda u, d: d["status"] == "PUBLISHED" and len((d.get("dialect") or {}).get("conflicts") or []) >= 2),
        ("Bought a plan during the day", lambda u, d: u in revised and d["arm"] == "agent" and d["status"] == "PUBLISHED"),
        ("App dialect has no content", lambda u, d: "no catalogue content" in (d.get("no_action_reason") or "")),
    ]
    found = {}
    for label, test in tests:
        for user_id, ds in run["by_viewer"].items():
            if test(user_id, ds[0]) and user_id not in found.values():
                found[label] = user_id
                break
    return found


# --------------------------------------------------------------------------- the loop
def run_agent(eng: dict, user_id: str, scenario: str) -> dict:
    """One live decision by the real agent on the viewer's first app open of the replay day."""
    settings = Settings()
    base = eng["first_trigger"][user_id]
    now = parse_ts(base["deliver_at"])
    trigger = dict(base, event_id=f"demo-{user_id}-{SCENARIOS.index(scenario)}", session_id=f"demo-{user_id}")
    if scenario == "Open arrives 2 days late":
        trigger["occurred_at"] = iso(parse_ts(base["sent_at"]) - timedelta(days=2))

    store = eng["store"].snapshot()
    for e in eng["live"].get(user_id, []):  # what had already arrived that day, e.g. a sign-up
        if e["deliver_at"] <= base["deliver_at"]:
            store.add_event(e)
    store.add_event(trigger)
    rules = RuleBook(eng["pack_before"].goals_config, settings, known_events=eng["known_events"])
    for c in eng["controls"]:
        if c["type"] == "goal_rule_corrected" and parse_ts(c["at"]) <= now:
            rules.apply_correction(c)
    withdrawn = [parse_ts(c["at"]) for c in eng["controls"] if c["type"] == "title_withdrawn"]
    pack = eng["pack_after"] if withdrawn and now >= min(withdrawn) else eng["pack_before"]

    faults = []
    if scenario == "Model gives a false answer":
        faults.append({"trigger_event_id": trigger["event_id"], "fault": "unsupported_claim",
                       "model_output": {"goal_id": "G04", "eligible": True, "confidence": 0.97,
                                        "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}})
    provider = MockProvider(faults)
    if scenario == "Model is down":
        provider.set_outage(now - timedelta(minutes=1), now + timedelta(minutes=10))
    if scenario == "Data store is down":
        store.degrade(now + timedelta(minutes=10), timeout_s=settings.store_degraded_timeout_s, timeout_rate=1.0,
                      slow_s=settings.store_degraded_slow_s)

    agent = ViewerAgent(user_id, Deps(pack, store, rules, settings, provider))
    decision, actions = agent.handle_trigger(trigger, now)
    second = agent.handle_trigger(trigger, now + timedelta(seconds=20))[0] if scenario == "Same open arrives twice" else None
    names = {t: pack.title(t).name for a in actions for t in [a.get("hero_title_id"), a.get("title_id")] if t and pack.title(t)}
    store.close()
    goal_text = {g["goal_id"]: g["description"] for g in pack.goals_config["goals"]}
    return {"decision": decision, "actions": actions, "second": second, "names": names, "goal_text": goal_text,
            "scenario": scenario}


def describe_action(a: dict, names: dict) -> str:
    if a["type"] == "home_page":
        return (f"Personalised home page in {a['dialect']} with {len(a['rows'])} rows, "
                f"hero: *{names.get(a['hero_title_id'], a['hero_title_id'])}*")
    when = parse_ts(a["send_at"]).astimezone(IST).strftime("%d %b %H:%M")
    channel = "WhatsApp" if a["type"] == "whatsapp" else "Push"
    return f"{channel} about *{names.get(a['title_id'], a['title_id'])}*, scheduled {when} IST"


def show_loop(result: dict, outcomes: list[dict]) -> None:
    d, actions = result["decision"], result["actions"]
    decide, act, measure = st.columns(3, border=True)

    with decide:
        st.markdown('<div class="step">1 · Decide</div>', unsafe_allow_html=True)
        goal = d.get("goal")
        if goal:
            st.markdown(f'<div class="big">{result["goal_text"].get(goal["id"], goal["name"])}</div>', unsafe_allow_html=True)
            reason = (d.get("eligibility") or {}).get("reason")
            st.caption(f"{goal['id']} {goal['name']} · why: {reason or 'kept the active goal'}")
            st.caption(f"Judged by: {goal['success_signal']}")
        else:
            st.markdown('<div class="big">No goal</div>', unsafe_allow_html=True)
            st.caption(d.get("no_goal_reason") or d.get("no_action_reason") or "")
        conf = d.get("confidence") or {}
        st.caption(f"Confidence {conf.get('value')} · {conf.get('reason', '')}")

    with act:
        st.markdown('<div class="step">2 · Act</div>', unsafe_allow_html=True)
        failed = (d.get("checks") or {}).get("failed") or []
        passed = (d.get("checks") or {}).get("passed") or []
        if actions:
            st.markdown('<div class="big">Published</div>', unsafe_allow_html=True)
            for a in actions:
                st.markdown(f"- {describe_action(a, result['names'])}")
        elif d["status"] == "HOLDOUT_NO_ACTION":
            st.markdown('<div class="big">Nothing sent (holdout)</div>', unsafe_allow_html=True)
            st.caption("Control group: the goal is recorded so the comparison is fair, but no action is allowed.")
        else:
            st.markdown('<div class="big">Chose to do nothing</div>', unsafe_allow_html=True)
            st.caption(d.get("no_action_reason") or "")
        if passed or failed:
            st.caption(f"✓ {len(passed)} independent checks passed" + (f" · ✗ {len(failed)} vetoed" if failed else ""))
        st.caption(f"Decided in {d['decided_in_ms']:,} ms (budget 60,000)")

    with measure:
        st.markdown('<div class="step">3 · Measure</div>', unsafe_allow_html=True)
        if result["scenario"] != "Normal":
            st.markdown('<div class="big">Not judged</div>', unsafe_allow_html=True)
            st.caption("A what-if run is not followed by 14 days of viewer behaviour, so there is nothing honest to "
                       "measure. Switch back to Normal to see this viewer's real outcome.")
        elif not outcomes:
            st.markdown('<div class="big">Nothing to judge</div>', unsafe_allow_html=True)
            st.caption("No goal was committed for this viewer in the replay.")
        else:
            o = outcomes[0]
            icon = {"succeeded": "✅", "failed": "❌"}.get(o["verdict"], "⏳")
            st.markdown(f'<div class="big">{icon} Goal {o["verdict"]}</div>', unsafe_allow_html=True)
            st.caption(o["reason"])
            st.caption({"exposed_before_success": "The viewer saw the agent's action before succeeding.",
                        "exposed_no_success": "The viewer saw the action but did not reach the goal.",
                        "published_but_never_seen": "The action was published but the viewer never saw it.",
                        "holdout_no_action": "Holdout: whatever happened, the agent did not cause it.",
                        "no_action_taken": "The agent did not act for this goal.",
                        "success_before_exposure": "The viewer succeeded before seeing anything from the agent.",
                        }.get(o["attribution"], o["attribution"]))
            st.caption("From the sample run: what the viewer did in the 14 days after.")

    # what went wrong and how the agent handled it, in one line
    model = d.get("model") or {}
    if model.get("rejected"):
        st.warning(f"**The model's answer was rejected.** {model['rejected']}", icon="🛡️")
    elif model.get("failure"):
        st.warning(f"**The model failed** ({model['failure']}). The agent decided with its rules instead.", icon="🛡️")
    elif d.get("fallback_used") == "store_degraded":
        st.warning("**The data store was down.** The agent waited its budget, then published only a safe "
                   "page that needs nothing about the viewer, and chose no goal.", icon="🛡️")
    if result.get("second"):
        st.info(f"**Second copy of the same open:** {result['second']['no_action_reason']}", icon="🔁")

    with st.expander("Evidence and checks"):
        dialect = d.get("dialect") or {}
        st.markdown(f"**Dialect:** `{dialect.get('used')}` from {dialect.get('source')}"
                    + (f", overriding {', '.join(dialect['conflicts'])}" if dialect.get("conflicts") else ""))
        for c in failed:
            st.markdown(f"✗ **{c['check'].replace('_', ' ')}**: {c['reason']}")
        if passed:
            st.markdown(":green[" + " · ".join(f"✓ {c.replace('_', ' ')}" for c in passed) + "]")
        if d.get("evidence"):
            st.caption("Evidence: " + ", ".join(f"`{e['ref']}`" for e in d["evidence"][:6]))
        for line in (d.get("conflicts") or []) + (d.get("assumptions") or []):
            st.caption(line)
    with st.expander("Decision record (JSON)"):
        st.json(d, expanded=False)


def show_impact(run: dict) -> None:
    first = {}
    for rows in run["outcomes"].values():
        o = min(rows, key=lambda o: o["started_at"])
        first[o["user_id"]] = o
    a = _rate([o for o in first.values() if o["arm"] == "agent"])
    h = _rate([o for o in first.values() if o["arm"] == "holdout"])
    diff, lo, hi = _diff_ci(a, h)
    left, right = st.columns([3, 2], vertical_alignment="center")
    with left:
        df = pd.DataFrame([{"arm": "agent", "rate": a["rate"], "viewers": a["judged"]},
                           {"arm": "holdout", "rate": h["rate"], "viewers": h["judged"]}])
        bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, size=26).encode(
            y=alt.Y("arm:N", title=None, axis=alt.Axis(labelFontSize=13)),
            x=alt.X("rate:Q", title="reached their goal", axis=alt.Axis(format="%")),
            color=alt.Color("arm:N", scale=alt.Scale(domain=["agent", "holdout"], range=[AGENT, HOLDOUT]), legend=None),
            tooltip=["arm", alt.Tooltip("rate:Q", format=".1%"), "viewers"])
        text = bars.mark_text(align="left", dx=6, fontSize=13).encode(text=alt.Text("rate:Q", format=".1%"),
                                                                       color=alt.value("#374151"))
        st.altair_chart((bars + text).properties(height=120), width="stretch")
    with right:
        if diff is None:
            st.markdown("**Not enough judged viewers in both arms to compare.**")
        else:
            verdict = "not proven yet" if lo <= 0 <= hi else ("better" if diff > 0 else "worse")
            st.markdown(f"**{_pp(diff)}** for the agent arm, 95% interval {_pp(lo)} to {_pp(hi)}: **{verdict}**.")
        st.caption("Randomized holdout, one goal per viewer. Comparing served vs unserved viewers would overstate "
                   "the effect, as the earlier pilot did.")


# --------------------------------------------------------------------------- page
st.title("One User, One Agent")
st.caption("Made by Satvik Pandey · one agent per viewer decides, acts and measures · mock model, no API key needed")

if not (RUN / "replay_summary.json").exists():
    st.error(f"No run found at `{RUN}`. Create one with `python -m viewer_agent run-all --pack ./pack --out sample_run`.")
    st.stop()
run = load_run(str(RUN))
s, lat, sim = run["summary"], run["latency"], run["sim"]
status = s["decisions_by_status"]
c = st.columns(4)
c[0].metric("App opens decided", f"{s['triggers']:,}", help=f"{s['viewers']:,} viewers on {s.get('replay_day_ist', 'the replay day')}")
c[1].metric("On time (≤ 60 s)", f"{100 * lat['within_budget'] / lat['decisions']:.0f}%", help=f"p95 {lat['p95_ms']} ms")
c[2].metric("Acted / chose not to", f"{status.get('PUBLISHED', 0):,} / {status.get('NO_ACTION', 0):,}",
            help=f"plus {status.get('HOLDOUT_NO_ACTION', 0):,} holdout and {status.get('DUPLICATE', 0)} duplicate opens")
c[3].metric("Policy violations", sim["policy_problem_count"], help="consent, quiet hours, daily cap, holdout: checked by the simulator")

st.subheader("Run the loop for one viewer")
if not (PACK / "data" / "events.jsonl").exists():
    st.warning(f"The live demo needs the data pack at `{PACK}` (or set VIEWER_AGENT_PACK).")
    st.stop()
engine = load_engine(str(PACK))
found = examples(run)
label_of = {u: label for label, u in found.items()}
viewers = list(found.values()) + sorted(u for u in engine["first_trigger"] if u not in label_of)
pick, what = st.columns([1, 1])
user_id = pick.selectbox("Viewer", viewers, key="viewer",
                         format_func=lambda u: f"{u} · {label_of[u]}" if u in label_of else u)
scenario = what.selectbox("What goes wrong?", SCENARIOS, key="scenario")
arm = engine["pack_before"].arms.get(user_id)
st.caption(f"Arm: **{arm}** · first app open of the day at "
           f"{parse_ts(engine['first_trigger'][user_id]['deliver_at']).astimezone(IST).strftime('%H:%M IST')}")

result = run_agent(engine, user_id, scenario)
st.session_state["last_result"] = result  # read by the tests
show_loop(result, run["outcomes"].get(user_id, []))

st.subheader("Did the agent help?")
show_impact(run)
