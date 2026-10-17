"""Streamlit demo for One User, One Agent.

    streamlit run app.py

Three tabs read the outputs of a run (sample_run/ by default). "Try the agent" runs the real agent live on the
data pack (./pack by default), on an in-memory copy of the store, so nothing in the saved run changes.
Paths can be overridden with VIEWER_AGENT_RUN and VIEWER_AGENT_PACK.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))  # so the app also runs without `pip install -e .`

from viewer_agent.agent import Deps, ViewerAgent  # noqa: E402
from viewer_agent.config import Settings  # noqa: E402
from viewer_agent.goals import RuleBook  # noqa: E402
from viewer_agent.judge import _diff_ci, _rate, pilot_reanalysis  # noqa: E402
from viewer_agent.pack import Pack  # noqa: E402
from viewer_agent.planner import MockProvider  # noqa: E402
from viewer_agent.replay import read_jsonl  # noqa: E402
from viewer_agent.store import Store  # noqa: E402
from viewer_agent.times import IST, UTC, iso, parse_ts  # noqa: E402

RUN = Path(os.environ.get("VIEWER_AGENT_RUN", ROOT / "sample_run"))
PACK = Path(os.environ.get("VIEWER_AGENT_PACK", ROOT / "pack"))
REPLAY_DAY = datetime(2026, 10, 1, tzinfo=IST)
AGENT, HOLDOUT = "#2a78d6", "#eb6834"

st.set_page_config(page_title="One User, One Agent", page_icon="🎬", layout="wide")
st.markdown("""<style>
  .block-container { padding-top: 2.2rem; max-width: 1240px; }
  [data-testid="stMetricValue"] { font-size: 1.6rem; }
  h1 { font-size: 1.9rem !important; } h3 { font-size: 1.15rem !important; }
</style>""", unsafe_allow_html=True)


# --------------------------------------------------------------------------- data
@st.cache_data(show_spinner=False)
def load_run(run_dir: str) -> dict:
    run = Path(run_dir)
    decisions = read_jsonl(run / "decisions.jsonl")
    by_viewer = defaultdict(list)
    for d in decisions:
        by_viewer[d["viewer_id"]].append(d)
    return {
        "summary": json.loads((run / "replay_summary.json").read_text(encoding="utf-8")),
        "latency": json.loads((run / "latency_report.json").read_text(encoding="utf-8")),
        "sim": json.loads((run / "sim" / "simulator_report.json").read_text(encoding="utf-8")),
        "decisions": decisions,
        "by_viewer": dict(by_viewer),
        "outcomes": read_jsonl(run / "outcomes.jsonl"),
    }


@st.cache_data(show_spinner=False)
def load_viewer(run_dir: str, user_id: str) -> dict | None:
    path = Path(run_dir) / "viewer_state" / f"{user_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


@st.cache_resource(show_spinner="Loading the data pack and 30 days of history…")
def load_engine(pack_dir: str) -> dict:
    """Everything the live agent needs, loaded once. The pack is loaded twice: before and after the
    mid-day title withdrawal, so a what-if at any hour sees the catalogue as it was then."""
    pack_dir = Path(pack_dir)
    settings = Settings()
    store = Store(":memory:", shared=True)  # read-only after this; each what-if works on a snapshot
    stats = store.ingest_history(pack_dir / "data" / "events.jsonl")
    controls = read_jsonl(pack_dir / "replay" / "control_events.jsonl")
    before, after = Pack(pack_dir), Pack(pack_dir)
    for p in (before, after):
        p.popularity = stats["plays_per_title"]
    for c in controls:
        if c["type"] == "title_withdrawn":
            after.withdraw(c["title_id"], parse_ts(c["at"]))
    first_trigger = {}
    for t in read_jsonl(pack_dir / "replay" / "triggers.jsonl"):
        first_trigger.setdefault(t["user_id"], t)
    return {"settings": settings, "store": store, "known_events": stats["event_names"], "controls": controls,
            "pack_before": before, "pack_after": after, "first_trigger": first_trigger}


def pack_ready() -> bool:
    return (PACK / "data" / "events.jsonl").exists()


def title_name(title_id: str | None) -> str:
    if not title_id:
        return "-"
    if pack_ready():
        t = load_engine(str(PACK))["pack_before"].title(title_id)
        if t:
            return t.name
    return title_id


def examples(run: dict) -> dict[str, str]:
    """One real viewer for each situation the brief cares about, found in the run itself."""
    revised = {r["user_id"] for r in run["summary"]["plan_revisions_from_live_events"]}
    reached = {o["user_id"] for o in run["outcomes"] if o["attribution"] == "exposed_before_success"}
    tests = [
        ("Acted, then the goal was reached", lambda u, ds: u in reached and any(len(d["actions"]) >= 2 for d in ds)),
        ("Holdout viewer: goal only, no action", lambda u, ds: any(d["status"] == "HOLDOUT_NO_ACTION" and d["goal"] for d in ds)),
        ("A purchase arrived after the decision", lambda u, ds: u in revised and ds[0]["arm"] == "agent"),
        ("App, profile and campaign disagree on dialect", lambda u, ds: ds[0]["arm"] == "agent"
         and any(len((d.get("dialect") or {}).get("conflicts") or []) >= 2 for d in ds)),
        ("Model claimed an ineligible goal", lambda u, ds: any(d["model"].get("rejected") for d in ds)),
        ("Model provider was down", lambda u, ds: any("503" in (d["model"].get("failure") or "") for d in ds)),
        ("Open buffered offline for days", lambda u, ds: ds[0]["arm"] == "agent" and any(
            d["trigger"].get("live_visit") is False and (d["trigger"].get("event_age_s") or 0) > 86400 for d in ds)),
        ("Same trigger delivered twice", lambda u, ds: any(d["status"] == "DUPLICATE" for d in ds)),
        ("App dialect has no content", lambda u, ds: any("no catalogue content" in (d.get("no_action_reason") or "") for d in ds)),
    ]
    found = {}
    for label, test in tests:
        for user_id, ds in run["by_viewer"].items():
            if test(user_id, ds):
                found[label] = user_id
                break
    return found


# --------------------------------------------------------------------------- small views
def status_line(d: dict) -> str:
    when = parse_ts(d["trigger"]["delivered_at"]).astimezone(IST).strftime("%d %b %H:%M IST")
    what = " + ".join(a.replace("_", " ").lower() for a in d["actions"]) if d["actions"] else "no action"
    return f"{when} · {d['status'].replace('_', ' ').lower()} · {what}"


def checks_md(checks: dict) -> str:
    passed = " · ".join(f"✓ {c.replace('_', ' ')}" for c in checks.get("passed", []))
    failed = "  \n".join(f"✗ **{c['check'].replace('_', ' ')}**: {c['reason']}" for c in checks.get("failed", []))
    return "  \n".join(x for x in (failed, f":green[{passed}]" if passed else "") if x) or "_no checks needed_"


def show_actions(actions: list[dict]) -> None:
    for a in actions:
        if a["type"] == "home_page":
            st.markdown(f"**Home page** in `{a['dialect']}`, hero: **{title_name(a.get('hero_title_id'))}**")
            rows = [{"row": r["row_type"].replace("_", " "),
                     "titles": ", ".join(title_name(t) for t in r["title_ids"][:4])
                     + (f"  +{len(r['title_ids']) - 4} more" if len(r["title_ids"]) > 4 else "")} for r in a["rows"]]
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        elif a["type"] == "whatsapp":
            st.markdown(f"**WhatsApp** at {iso(parse_ts(a['send_at']).astimezone(IST))[:16].replace('T', ' ')} IST · "
                        f"template `{a['template_id']}` · {a['variables']}")
        else:
            st.markdown(f"**Push** at {iso(parse_ts(a['send_at']).astimezone(IST))[:16].replace('T', ' ')} IST · "
                        f"“{a['text']['title']}” – {a['text']['body']}")


def explain_decision(d: dict, actions: list[dict]) -> None:
    left, right = st.columns([3, 2])
    with left:
        goal = d.get("goal")
        if goal:
            shadow = " (shadow goal, holdout)" if goal.get("shadow") else ""
            st.markdown(f"**Goal:** {goal['id']} `{goal['name']}`{shadow}  \n"
                        f"Judged by: {goal['success_signal']}, deadline {goal['deadline'][:16].replace('T', ' ')} UTC")
            if d.get("eligibility"):
                st.caption(f"Why eligible: {d['eligibility']['reason']}")
        else:
            st.markdown(f"**No goal:** {d.get('no_goal_reason') or d.get('no_action_reason') or '-'}")
        dialect = d.get("dialect") or {}
        line = f"**Dialect used:** `{dialect.get('used')}` from {dialect.get('source')}"
        if dialect.get("conflicts"):
            line += f", overriding {', '.join(dialect['conflicts'])}"
        st.markdown(line)
        if actions:
            show_actions(actions)
        else:
            st.info(f"Did nothing: {d.get('no_action_reason')}")
    with right:
        st.markdown("**Independent checks**")
        st.markdown(checks_md(d.get("checks") or {}))
        conf = d.get("confidence") or {}
        st.markdown(f"**Confidence** {conf.get('value')}  \n<small>{conf.get('reason', '')}</small>", unsafe_allow_html=True)
        model = d.get("model") or {}
        if model.get("rejected"):
            st.warning(f"Model answer rejected: {model['rejected']}")
        elif model.get("failure"):
            st.warning(f"Model unavailable ({model['failure']}); rules decided")
        st.caption(f"Decided in {d['decided_in_ms']:,} ms · {d['budget']['model_calls']} model / "
                   f"{d['budget']['tool_calls']} tool calls")


# --------------------------------------------------------------------------- tabs
def tab_overview(run: dict) -> None:
    s, lat, sim = run["summary"], run["latency"], run["sim"]
    st_ = s["decisions_by_status"]
    st.caption("The replay day, 1 Oct 2026: every app open handled by that viewer's agent.")
    c = st.columns(5)
    c[0].metric("App opens", f"{s['triggers']:,}", help=f"{s['viewers']:,} viewers", border=True)
    c[1].metric("Acted", f"{st_.get('PUBLISHED', 0):,}", help="home page and/or one message", border=True)
    c[2].metric("Chose to do nothing", f"{st_.get('NO_ACTION', 0):,}", help="every one has a recorded reason", border=True)
    c[3].metric("Holdout (goal only)", f"{st_.get('HOLDOUT_NO_ACTION', 0):,}", help="randomized control: nothing sent", border=True)
    c[4].metric("Policy violations", sim["policy_problem_count"], help="checked by the platform simulator", border=True)
    c = st.columns(5)
    c[0].metric("Decided within 60 s", f"{lat['within_budget']:,}/{lat['decisions']:,}", border=True)
    c[1].metric("p95 decision time", f"{lat['p95_ms']} ms", help=f"max {lat['max_ms']:,} ms during an outage", border=True)
    c[2].metric("Duplicates caught", f"{st_.get('DUPLICATE', 0):,}", border=True)
    c[3].metric("Model claims rejected", s["model"]["advice_rejected"], border=True)
    c[4].metric("Model failures survived", s["model"]["failures"], border=True)

    left, right = st.columns([1, 1], gap="large")
    with left:
        st.subheader("Why it did nothing")
        names = {"holdout": "holdout arm", "buffered offline": "opened offline, arrived late",
                 "still live": "page from this visit still live", "no catalogue content": "no content in app dialect",
                 "duplicate": "duplicate delivery", "dialect unknown": "app dialect missing",
                 "no measurable goal": "no measurable goal"}
        df = pd.DataFrame([{"reason": names.get(k, k), "decisions": v} for k, v in s["no_action_reasons"].items()])
        chart = alt.Chart(df).mark_bar(color=AGENT, cornerRadiusEnd=4, size=16).encode(
            x=alt.X("decisions:Q", title=None), y=alt.Y("reason:N", sort="-x", title=None, axis=alt.Axis(labelLimit=260)),
            tooltip=["reason", "decisions"])
        st.altair_chart(chart, width="stretch")
    with right:
        st.subheader("Surprises it handled")
        rows = []
        for c_ in s["control_events"]:
            at = parse_ts(c_["at"]).astimezone(IST).strftime("%H:%M")
            if c_["type"] == "title_withdrawn":
                rows.append((at, f"Title {c_['title_id']} withdrawn", f"{len(c_['live_pages_republished'])} live pages republished without it"))
            elif c_["type"] == "goal_rule_corrected":
                rows.append((at, f"{c_['goal_id']} success rule corrected", f"only open {c_['goal_id']} goals touched ({c_['open_goals_affected']})"))
            else:
                rows.append((at, f"{c_['dependency'].replace('_', ' ')} {c_['type'].split('_')[1]}", "rule-based or smaller safe action, on time"))
        rows.append(("all day", "Purchases arriving after a decision", f"{len(s['plan_revisions_from_live_events'])} goals closed early"))
        rows.append(("all day", "Offline-buffered and duplicate opens", "recognised, recorded, not acted on"))
        st.dataframe(pd.DataFrame(rows, columns=["IST", "what happened", "response"]), hide_index=True, width="stretch")


def tab_viewer(run: dict) -> None:
    found = examples(run)
    left, right = st.columns([2, 1])
    choice = left.selectbox("Pick a situation", list(found) + ["Any viewer…"])
    if choice == "Any viewer…":
        user_id = right.selectbox("Viewer", sorted(run["by_viewer"]))
    else:
        user_id = found[choice]
        right.text_input("Viewer", user_id, disabled=True)

    v = load_viewer(str(RUN), user_id) or {"state": {}, "decisions": run["by_viewer"].get(user_id, []),
                                                      "actions": [], "outcomes": []}
    decisions, outcomes = v["decisions"], v["outcomes"]
    arm = (v.get("state") or {}).get("arm") or decisions[0]["arm"]
    c = st.columns(4)
    c[0].metric("Arm", arm)
    c[1].metric("Decisions", len(decisions))
    c[2].metric("Actions", len(v["actions"]))
    c[3].metric("Outcome", outcomes[0]["verdict"] if outcomes else "no goal")

    for i, d in enumerate(decisions):
        with st.expander(status_line(d), expanded=i == 0):
            explain_decision(d, [a for a in v["actions"] if a["decision_id"] == d["decision_id"]])

    for o in outcomes:
        text = f"**{o['goal_id']} {o['goal_name']}: {o['verdict']}** · {o['reason']} · attribution: {o['attribution'].replace('_', ' ')}"
        (st.success if o["verdict"] == "succeeded" else st.error if o["verdict"] == "failed" else st.info)(text)

    facts = (v.get("state") or {}).get("facts") or {}
    if facts:
        with st.expander("What the agent knew, with source and time"):
            st.dataframe(pd.DataFrame([{"fact": k.replace("_", " "), "value": json.dumps(f["value"], default=str)[:120],
                                        "source": f["source"], "observed": (f["observed_at"] or "")[:16].replace("T", " ")}
                                       for k, f in facts.items()]), hide_index=True, width="stretch")
    learnings = (v.get("state") or {}).get("learnings") or []
    if learnings:
        st.caption("Stored for next time: " + " | ".join(learnings[-2:]))


def tab_try(run: dict) -> None:
    st.caption("Run the real agent on one app open and change the situation. Nothing in the saved run is modified.")
    if not pack_ready():
        st.warning(f"This tab needs the data pack at `{PACK}` (or set VIEWER_AGENT_PACK).")
        return
    eng = load_engine(str(PACK))
    found = examples(run)
    viewers = sorted(eng["first_trigger"])
    default = found.get("Acted, then the goal was reached", viewers[0])

    with st.form("try"):
        a, b, c = st.columns(3)
        with a:
            user_id = st.selectbox("Viewer", viewers, index=viewers.index(default), key="try_viewer")
            arm = eng["pack_before"].arms.get(user_id)
            st.caption(f"Randomized arm: **{arm}**" + (" — the agent will choose a goal but must not act." if arm == "holdout" else ""))
            hour = st.slider("Opens the app at (IST)", 0, 23, 20, key="try_hour")
        with b:
            dialect = st.selectbox("App dialect", ["as recorded", "d1", "d2", "d3", "d4", "d5", "d6", "d7 (no content)", "missing"], key="try_dialect")
            offline_h = st.number_input("Open was buffered offline for (hours)", 0, 96, 0, key="try_offline")
        with c:
            st.markdown("**Make something go wrong**")
            outage = st.checkbox("Model provider down (HTTP 503)", key="try_outage")
            claim = st.checkbox("Model claims an ineligible goal", key="try_claim")
            broken = st.checkbox("Model returns broken JSON", key="try_broken")
            store_down = st.checkbox("Viewer data store unavailable", key="try_store")
            twice = st.checkbox("Same open delivered twice", key="try_twice")
        submitted = st.form_submit_button("Run the agent", type="primary", key="try_run")

    if submitted:
        st.session_state["try_result"] = run_agent(eng, user_id, hour, dialect, offline_h, outage, claim, broken, store_down, twice)
    result = st.session_state.get("try_result")
    if not result:
        st.info("Pick a situation and press **Run the agent**.")
        return

    d, actions = result["decision"], result["actions"]
    if d["status"] == "PUBLISHED":
        st.success(f"**Acted:** {' + '.join(x.replace('_', ' ').lower() for x in d['actions'])}")
    elif d["status"] == "HOLDOUT_NO_ACTION":
        st.warning(f"**Holdout:** goal recorded, nothing published. {d.get('no_action_reason') or ''}")
    else:
        st.info(f"**{d['status'].replace('_', ' ').title()}:** {d.get('no_action_reason')}")
    explain_decision(d, actions)
    if result.get("duplicate"):
        st.markdown(f"**Second delivery of the same open:** {result['duplicate']['status']} — {result['duplicate']['no_action_reason']}")
    with st.expander("Decision record (machine-readable, as the brief asks)"):
        st.json(d, expanded=False)


def run_agent(eng: dict, user_id: str, hour: int, dialect: str, offline_h: int, outage: bool, claim: bool,
              broken: bool, store_down: bool, twice: bool) -> dict:
    """One live decision with the real agent, verifier and rules, on a private copy of the store."""
    settings: Settings = eng["settings"]
    now = (REPLAY_DAY + timedelta(hours=hour, minutes=5)).astimezone(UTC)
    base = eng["first_trigger"][user_id]
    trigger = dict(base, event_id=f"whatif-{user_id}-{hour}-{offline_h}-{int(outage)}{int(claim)}{int(broken)}{int(store_down)}",
                   session_id=f"whatif-{user_id}", occurred_at=iso(now - timedelta(hours=offline_h)), sent_at=iso(now),
                   received_at=iso(now), deliver_at=iso(now))
    if dialect == "missing":
        trigger["app_dialect"] = None
    elif dialect != "as recorded":
        trigger["app_dialect"] = dialect.split()[0]

    store = eng["store"].snapshot()
    store.add_event(trigger)
    rules = RuleBook(eng["pack_before"].goals_config, settings, known_events=eng["known_events"])
    for c in eng["controls"]:
        if c["type"] == "goal_rule_corrected" and parse_ts(c["at"]) <= now:
            rules.apply_correction(c)
    withdrawn_at = min((parse_ts(c["at"]) for c in eng["controls"] if c["type"] == "title_withdrawn"), default=None)
    pack = eng["pack_after"] if withdrawn_at and now >= withdrawn_at else eng["pack_before"]

    faults = []
    if claim:
        faults.append({"trigger_event_id": trigger["event_id"], "fault": "unsupported_claim",
                       "model_output": {"goal_id": "G04", "eligible": True, "confidence": 0.97,
                                        "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}})
    elif broken:
        faults.append({"trigger_event_id": trigger["event_id"], "fault": "malformed_output",
                       "model_output": '{"goal_id": "G02", "eligible": tr'})
    provider = MockProvider(faults)
    if outage:
        provider.set_outage(now - timedelta(minutes=1), now + timedelta(minutes=10))
    if store_down:
        store.degrade(now + timedelta(minutes=10), timeout_s=settings.store_degraded_timeout_s, timeout_rate=1.0,
                      slow_s=settings.store_degraded_slow_s)

    agent = ViewerAgent(user_id, Deps(pack, store, rules, settings, provider))
    decision, actions = agent.handle_trigger(trigger, now)
    duplicate = agent.handle_trigger(trigger, now + timedelta(seconds=20))[0] if twice else None
    store.close()
    return {"decision": decision, "actions": actions, "duplicate": duplicate}


def tab_impact(run: dict) -> None:
    first = {}
    for o in sorted(run["outcomes"], key=lambda o: o["started_at"]):
        first.setdefault(o["user_id"], o)
    rows = list(first.values())
    goals = sorted({o["goal_id"] for o in rows})
    names = {o["goal_id"]: o["goal_name"] for o in rows}
    pick = st.selectbox("Goal", ["All goals"] + goals, format_func=lambda g: g if g == "All goals" else f"{g} {names[g]}")
    rows = rows if pick == "All goals" else [o for o in rows if o["goal_id"] == pick]
    a, h = _rate([o for o in rows if o["arm"] == "agent"]), _rate([o for o in rows if o["arm"] == "holdout"])
    d, lo, hi = _diff_ci(a, h)

    pct = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"  # noqa: E731
    pp = lambda x: "n/a" if x is None else f"{100 * x:+.1f} pp"  # noqa: E731
    c = st.columns(3)
    c[0].metric("Agent arm reached goal", pct(a["rate"]), help=f"{a['succeeded']} of {a['judged']} viewers", border=True)
    c[1].metric("Holdout arm reached goal", pct(h["rate"]), help=f"{h['succeeded']} of {h['judged']} viewers", border=True)
    c[2].metric("Difference", pp(d), help=f"95% interval {pp(lo)} to {pp(hi)}", border=True)

    left, right = st.columns([1, 1], gap="large")
    with left:
        df = pd.DataFrame([{"arm": "agent", "rate": a["rate"] or 0, "n": a["judged"]},
                           {"arm": "holdout", "rate": h["rate"] or 0, "n": h["judged"]}])
        bars = alt.Chart(df).mark_bar(cornerRadiusEnd=4, size=70).encode(
            x=alt.X("arm:N", title=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("rate:Q", title="reached goal", axis=alt.Axis(format="%")),
            color=alt.Color("arm:N", scale=alt.Scale(domain=["agent", "holdout"], range=[AGENT, HOLDOUT]), legend=None),
            tooltip=["arm", alt.Tooltip("rate:Q", format=".1%"), alt.Tooltip("n:Q", title="viewers")])
        labels = bars.mark_text(dy=-8).encode(text=alt.Text("rate:Q", format=".1%"), color=alt.value("#52514e"))
        st.altair_chart((bars + labels).properties(height=260), width="stretch")
    with right:
        if d is None:
            st.info("Not enough viewers in both arms for this goal.")
        elif lo <= 0 <= hi:
            st.info(f"**Not proven.** The 95% interval ({pp(lo)} to {pp(hi)}) includes zero, so this run does not show "
                    "that the agent changed the outcome. Reporting that honestly is the point of the holdout.")
        elif d > 0:
            st.success(f"The 95% interval ({pp(lo)} to {pp(hi)}) is above zero: the agent arm did better.")
        else:
            st.error(f"The 95% interval ({pp(lo)} to {pp(hi)}) is below zero: the agent arm did worse.")
        st.caption("Both arms got a goal from the same rules; only the agent arm could act. One row per viewer.")

    with st.expander("Why “served vs not served” would mislead"):
        agent_rows = [o for o in first.values() if o["arm"] == "agent"]
        seen = _rate([o for o in agent_rows if o["attribution"].startswith("exposed")])
        unseen = _rate([o for o in agent_rows if not o["attribution"].startswith("exposed")])
        st.markdown(f"Inside the agent arm, viewers who **saw** an agent page reached their goal {pct(seen['rate'])} of the time, "
                    f"those who did not {pct(unseen['rate'])}. That gap is mostly self-selection: only viewers who come back "
                    "can see a page, and they succeed more anyway.")
        pilot = pilot_reanalysis(PACK) if pack_ready() else None
        if pilot:
            st.markdown(f"The earlier pilot made this mistake: served {pct(pilot['served'][1])} vs everyone else "
                        f"{pct(pilot['not_served'][1])} was reported as a 2.3x uplift. By randomized arm it was "
                        f"{pct(pilot['treatment'][1])} vs {pct(pilot['holdout'][1])}.")


# --------------------------------------------------------------------------- page
st.title("One User, One Agent")
st.caption("Made by Satvik Pandey · one agent per viewer: pick a measurable goal, act or deliberately do nothing, "
           "verify, then measure against a randomized holdout · mock model provider")

if not (RUN / "replay_summary.json").exists():
    st.error(f"No run found at `{RUN}`. Create one with `python -m viewer_agent run-all --pack ./pack --out sample_run`.")
    st.stop()

run_data = load_run(str(RUN))
overview, viewer, try_it, impact = st.tabs(["Overview", "Explore a viewer", "Try the agent", "Impact vs holdout"])
with overview:
    tab_overview(run_data)
with viewer:
    tab_viewer(run_data)
with try_it:
    tab_try(run_data)
with impact:
    tab_impact(run_data)
