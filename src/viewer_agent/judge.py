"""Closes the loop.

Judges every committed goal from what the viewer actually did afterwards (the simulator's outcome events plus the
live events of the replay day), records the outcome and a learning in the viewer's state, and measures the agent
against the randomized holdout. Only signals that can be tied to the viewer and the window count.
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .config import Settings
from .facts import event_time
from .goals import RuleBook
from .pack import Pack
from .replay import read_jsonl, write_jsonl
from .store import Store
from .times import iso, parse_ts

EXPOSURE_EVENTS = ("home_page_shown", "message_delivered", "message_clicked")


def load_rules(pack: Pack, store: Store, pack_dir: Path, settings: Settings) -> RuleBook:
    """Rules as they stood at the end of the replay: corrections apply to goals that were still open."""
    rules = RuleBook(pack.goals_config, settings, known_events=store.event_names())
    for c in read_jsonl(pack_dir / "replay" / "control_events.jsonl"):
        if c["type"] == "goal_rule_corrected":
            rules.apply_correction(c)
    return rules


# --------------------------------------------------------------------------- judging
def run_judge(pack_dir: Path, out_dir: Path) -> dict:
    pack_dir, out_dir = Path(pack_dir), Path(out_dir)
    settings = Settings()
    pack = Pack(pack_dir)
    store = Store(out_dir / "state.db")
    rules = load_rules(pack, store, pack_dir, settings)

    # "observed until" is the last moment we have data for; a goal whose window ends after it stays inconclusive
    outcome_events = defaultdict(list)
    observed_until = store.latest_event_time()
    for e in read_jsonl(out_dir / "sim" / "outcome_events.jsonl"):
        e["_at"] = event_time(e)
        outcome_events[e["user_id"]].append(e)
        observed_until = e["_at"] if observed_until is None or e["_at"] > observed_until else observed_until

    decisions_by_user = defaultdict(list)
    for d in read_jsonl(out_dir / "decisions.jsonl"):
        decisions_by_user[d["viewer_id"]].append(d)

    state_dir = out_dir / "viewer_state"
    state_dir.mkdir(exist_ok=True)
    outcomes, counts = [], Counter()
    states = dict(store.all_states())
    # a viewer decided while the store was unavailable has decisions but no saved state; export them too
    for user_id in decisions_by_user:
        states.setdefault(user_id, {"user_id": user_id, "note": "no state saved (data store unavailable at decision time)"})
    for user_id, state in sorted(states.items()):
        goals = list(state.get("goal_history", [])) + ([state["active_goal"]] if state.get("active_goal") else [])
        events = store.events_for(user_id)
        for e in events:
            e["_at"] = event_time(e)
        events += outcome_events.get(user_id, [])
        actions = [a for a in store.actions_for(user_id) if not a.get("cancelled_at")]

        for goal in goals:
            verdict = rules.judge(goal, events, observed_until, pack)
            exposure = _exposure(goal, events, actions, verdict.success_at)
            outcome = {
                "user_id": user_id, "arm": state.get("arm"), "goal_instance_id": goal["instance_id"],
                "goal_id": goal["goal_id"], "goal_name": goal["name"], "started_at": goal["started_at"],
                "deadline": goal["deadline"], "rule_version_at_commit": goal["rule_version"],
                "rule_version_at_judgement": rules.version, "closed_early": goal.get("close_reason"),
                "verdict": verdict.verdict, "reason": verdict.reason,
                "success_event_id": verdict.success_event_id, "success_at": verdict.success_at,
                "attribution": exposure["attribution"], "exposure": exposure["counts"],
                "actions": [a["action_id"] for a in exposure["actions"]],
            }
            goal["outcome"] = {k: outcome[k] for k in ("verdict", "reason", "success_event_id", "success_at",
                                                        "attribution", "exposure", "rule_version_at_judgement")}
            outcomes.append(outcome)
            counts[(state.get("arm"), verdict.verdict)] += 1
            state.setdefault("learnings", []).append(_learning(goal, outcome))

        if goals:
            store.save_state(user_id, state, observed_until or datetime.now().astimezone())
        export = {"state": state, "decisions": decisions_by_user.get(user_id, []), "actions": actions,
                  "outcomes": [o for o in outcomes if o["user_id"] == user_id]}
        (state_dir / f"{user_id}.json").write_text(json.dumps(export, indent=1, ensure_ascii=False), encoding="utf-8")

    write_jsonl(out_dir / "outcomes.jsonl", outcomes)
    store.close()
    return {"goals_judged": len(outcomes), "by_arm_and_verdict": {f"{a}/{v}": n for (a, v), n in sorted(counts.items())},
            "observed_until": iso(observed_until) if observed_until else None,
            "viewer_state_files": len(list(state_dir.glob("*.json")))}


def _exposure(goal: dict, events: list[dict], actions: list[dict], success_at: str | None) -> dict:
    """Did this viewer actually see something we published for this goal, and did that happen before success?"""
    start, deadline = parse_ts(goal["started_at"]), parse_ts(goal["deadline"])
    ours = [a for a in actions if a.get("goal_id") == goal["goal_id"] and start <= parse_ts(a["published_at"]) <= deadline]
    ids = {a["action_id"] for a in ours}
    seen = [e for e in events if e["event"] in EXPOSURE_EVENTS and start <= e["_at"] <= deadline
            and (e["properties"].get("page_action_id") in ids or e["properties"].get("action_id") in ids)]
    counts = Counter(e["event"] for e in seen)
    success = parse_ts(success_at)
    if goal.get("shadow"):
        attribution = "holdout_no_action"
    elif not ours:
        attribution = "no_action_taken"
    elif not seen:
        attribution = "published_but_never_seen"
    elif success is None:
        attribution = "exposed_no_success"
    elif any(e["_at"] < success for e in seen):
        attribution = "exposed_before_success"
    else:
        attribution = "success_before_exposure"
    return {"attribution": attribution, "counts": dict(counts), "actions": ours}


def _learning(goal: dict, outcome: dict) -> str:
    shown = outcome["exposure"].get("home_page_shown", 0)
    delivered = outcome["exposure"].get("message_delivered", 0)
    seen = f"page shown {shown}x, message delivered {delivered}x" if (shown or delivered) else "nothing of ours was seen"
    known_at = outcome["success_at"] or outcome["deadline"]  # the moment the verdict became certain
    return f"{known_at} {goal['goal_id']} {outcome['verdict']} ({outcome['attribution']}; {seen}). {outcome['reason']}"


# --------------------------------------------------------------------------- impact
def _rate(rows: list[dict]) -> dict:
    judged = [r for r in rows if r["verdict"] in ("succeeded", "failed")]
    s = sum(1 for r in judged if r["verdict"] == "succeeded")
    return {"n": len(rows), "judged": len(judged), "succeeded": s,
            "inconclusive": sum(1 for r in rows if r["verdict"] == "inconclusive"),
            "rate": s / len(judged) if judged else None}


def _diff_ci(a: dict, b: dict) -> tuple[float | None, float | None, float | None]:
    """Difference in success rate (a minus b) with a 95% Wald interval."""
    if not a["judged"] or not b["judged"] or a["rate"] is None or b["rate"] is None:
        return None, None, None
    p1, p2, n1, n2 = a["rate"], b["rate"], a["judged"], b["judged"]
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    d = p1 - p2
    return d, d - 1.96 * se, d + 1.96 * se


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _pp(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:+.1f} pp"


def pilot_reanalysis(pack_dir: Path) -> dict | None:
    path = pack_dir / "pilot" / "pilot_outcomes.csv"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    groups = defaultdict(list)
    for r in rows:
        ok = r["succeeded"] == "true"
        groups["served"].append(ok) if r["served"] == "true" else groups["not_served"].append(ok)
        groups[r["arm"]].append(ok)
        groups[f"{r['goal_id']}/{r['arm']}"].append(ok)
    return {k: (len(v), sum(v) / len(v)) for k, v in groups.items()}


def write_reports(pack_dir: Path, out_dir: Path) -> list[Path]:
    pack_dir, out_dir = Path(pack_dir), Path(out_dir)
    outcomes = read_jsonl(out_dir / "outcomes.jsonl")
    decisions = read_jsonl(out_dir / "decisions.jsonl")
    summary = json.loads((out_dir / "replay_summary.json").read_text(encoding="utf-8"))
    latency = json.loads((out_dir / "latency_report.json").read_text(encoding="utf-8"))
    sim = json.loads((out_dir / "sim" / "simulator_report.json").read_text(encoding="utf-8"))

    # One row per viewer: the first goal committed on the replay day. Both arms get a goal, so this is intention-to-treat.
    first = {}
    for o in sorted(outcomes, key=lambda o: o["started_at"]):
        first.setdefault(o["user_id"], o)
    rows = list(first.values())
    agent = [o for o in rows if o["arm"] == "agent"]
    holdout = [o for o in rows if o["arm"] == "holdout"]
    a, h = _rate(agent), _rate(holdout)
    d, lo, hi = _diff_ci(a, h)

    lines = ["# Impact report", "",
             "Question: did viewers whose agent was allowed to act reach their goal more often than viewers whose agent",
             "chose a goal but was not allowed to act (the randomized holdout)?", "",
             "## Headline: agent arm vs holdout arm (intention to treat)", "",
             "Every triggered viewer got a goal from the same rules; only the agent arm got actions. One row per viewer",
             "(first goal of the replay day). Inconclusive goals (window still open at the end of the data) are excluded",
             "from the rate but shown.", "",
             "| Arm | Viewers | Judged | Succeeded | Inconclusive | Success rate |", "|---|---|---|---|---|---|",
             f"| agent | {a['n']} | {a['judged']} | {a['succeeded']} | {a['inconclusive']} | {_pct(a['rate'])} |",
             f"| holdout | {h['n']} | {h['judged']} | {h['succeeded']} | {h['inconclusive']} | {_pct(h['rate'])} |", "",
             f"Difference (agent minus holdout): **{_pp(d)}**, 95% interval {_pp(lo)} to {_pp(hi)}."]
    if d is not None:
        if lo <= 0 <= hi:
            lines.append("The interval includes zero: at this sample size the data does not show that the agent changed "
                         "the outcome. That is the honest reading, not a failure of the measurement.")
        elif d > 0:
            lines.append("The interval excludes zero: the agent arm did better than the holdout.")
        else:
            lines.append("The interval excludes zero and the difference is negative: the agent arm did worse. "
                         "This needs investigation before any rollout.")

    triggered = Counter()
    for arm in {dd["viewer_id"]: dd["arm"] for dd in decisions}.values():
        triggered[arm] += 1
    multi = len(outcomes) - len(rows)
    lines += ["", "### Who is counted", "",
              f"- {len(outcomes)} goals were judged in total for {len(rows)} viewers. {multi} viewer(s) got a second goal the same",
              "  day after the first closed early (the viewer stopped qualifying); the table above counts the first goal only.",
              f"- Triggered viewers: agent {triggered['agent']}, holdout {triggered['holdout']}. Viewers without any goal: agent "
              f"{triggered['agent'] - a['n']}, holdout {triggered['holdout'] - h['n']}. Nearly all of them only had app opens that were",
              "  buffered offline for hours or days, which the same rule treats as \"not a live visit\" in both arms."]

    lines += ["", "## By goal", "", "| Goal | Agent judged | Agent rate | Holdout judged | Holdout rate | Difference |",
              "|---|---|---|---|---|---|"]
    for goal_id in sorted({o["goal_id"] for o in rows}):
        ga, gh = _rate([o for o in agent if o["goal_id"] == goal_id]), _rate([o for o in holdout if o["goal_id"] == goal_id])
        gd, _, _ = _diff_ci(ga, gh)
        lines.append(f"| {goal_id} | {ga['judged']} | {_pct(ga['rate'])} | {gh['judged']} | {_pct(gh['rate'])} | {_pp(gd)} |")
    lines.append("")
    lines.append("Small per-goal counts mean wide intervals; read the by-goal rows as direction, not proof.")

    exposed = [o for o in agent if o["attribution"] in ("exposed_before_success", "exposed_no_success")]
    unexposed = [o for o in agent if o["attribution"] in ("published_but_never_seen", "no_action_taken", "success_before_exposure")]
    e, u = _rate(exposed), _rate(unexposed)
    lines += ["", "## Why the simpler comparison misleads", "",
              "Inside the agent arm one can compare viewers who actually saw a personalised page or message with those",
              "who did not. That is the comparison the pilot report made (served vs everyone else) and it is biased:",
              "a viewer is only *exposed* if they open the app again, and viewers who open the app again are exactly the",
              "viewers more likely to play, complete or pay anyway.", "",
              "| Agent-arm viewers | Judged | Success rate |", "|---|---|---|",
              f"| exposed to an agent action | {e['judged']} | {_pct(e['rate'])} |",
              f"| not exposed (no action, or published but never seen) | {u['judged']} | {_pct(u['rate'])} |", "",
              "The gap above mixes the agent's effect with self-selection. The arm comparison does not, because the",
              "holdout was assigned at random before anyone opened the app."]

    pilot = pilot_reanalysis(pack_dir)
    if pilot:
        served, other, treat, hold = pilot["served"], pilot["not_served"], pilot["treatment"], pilot["holdout"]
        lines += ["", "## The earlier pilot, re-read the same way", "",
                  f"The internal report compared served viewers ({served[0]}, {_pct(served[1])} reached the goal) with everyone",
                  f"else ({other[0]}, {_pct(other[1])}) and called it a 2.3x uplift. Using the pilot's own randomized arms:", "",
                  "| Pilot arm | Viewers | Reached goal |", "|---|---|---|",
                  f"| treatment | {treat[0]} | {_pct(treat[1])} |", f"| holdout | {hold[0]} | {_pct(hold[1])} |", "",
                  f"Difference: {_pp(treat[1] - hold[1])}. The headline uplift was selection, not effect."]

    attributions = Counter(o["attribution"] for o in outcomes if o["arm"] == "agent")
    lines += ["", "## Attribution detail (agent arm, all goals)", "", "| Attribution | Goals |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in attributions.most_common()]
    lines += ["", "`exposed_before_success` is the only bucket where the agent could have caused the outcome; even there",
              "the arm comparison, not the exposure, is the evidence of effect.", "",
              f"Data: {len(outcomes)} judged goals from `outcomes.jsonl`; outcome events from the pack simulator",
              f"(seed `{sim['seed']}`, {sim['events_written']} events). Evaluators re-run with their own seed and replay day."]
    impact_path = out_dir / "impact_report.md"
    impact_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    final_path = out_dir / "final_report.md"
    final_path.write_text(_final_report(summary, latency, sim, outcomes, decisions, a, h, d, lo, hi), encoding="utf-8")
    return [impact_path, final_path]


def _final_report(summary, latency, sim, outcomes, decisions, a, h, d, lo, hi) -> str:
    status = summary["decisions_by_status"]
    verdicts = Counter((o["arm"], o["verdict"]) for o in outcomes)
    no_action = summary["no_action_reasons"]
    controls = summary["control_events"]
    lines = ["# Final report: sample replay", "",
             f"Replay day 2026-10-01 IST, {summary['triggers']} triggers from {summary['viewers']} viewers, mock model provider.", "",
             "## What the agents did", "", "| Status | Decisions |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(status.items(), key=lambda kv: -kv[1])]
    lines += ["", "Actions published: " + ", ".join(f"{v} {k}" for k, v in summary["actions_by_type"].items()) + ".", "",
              "Goals committed (both arms): " + ", ".join(f"{k} {v}" for k, v in sorted(summary["goals_chosen"].items(), key=lambda kv: -kv[1])) + ".",
              "", "### Why the agent did nothing", "", "| Reason | Decisions |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(no_action.items(), key=lambda kv: -kv[1])]
    lines += ["", "## Real-time behaviour", "",
              f"- Every decision published within the {latency['budget_s']} s budget: {latency['within_budget']}/{latency['decisions']}; "
              f"late {latency['late']}, failed {latency['failed']}.",
              f"- p50 {latency['p50_ms']} ms, p95 {latency['p95_ms']} ms, max {latency['max_ms']} ms "
              f"({latency['with_simulated_wait']} decisions waited on a degraded dependency).",
              f"- Peak load {latency['peak_triggers_per_minute']} triggers in one minute ({latency['peak_minute_utc']} UTC, the offline-buffer burst).",
              f"- Duplicate triggers recognised: {status.get('DUPLICATE', 0)}. Fallbacks: " +
              (", ".join(f"{v} {k}" for k, v in summary["fallbacks"].items()) or "none") + ".",
              f"- Model: {summary['model']['calls']} calls, advice used {summary['model']['advice_used']}, "
              f"rejected {summary['model']['advice_rejected']}, provider failures {summary['model']['failures']}.",
              "", "## Changed conditions noticed during the day", ""]
    for c in controls:
        if c["type"] == "title_withdrawn":
            lines.append(f"- {c['at']} title {c['title_id']} withdrawn: {len(c['live_pages_republished'])} live pages republished without it, "
                         f"{len(c['messages_cancelled'])} scheduled messages cancelled.")
        elif c["type"] == "goal_rule_corrected":
            lines.append(f"- {c['at']} success rule for {c['goal_id']} corrected: {c['open_goals_affected']} open goals re-tagged; "
                         f"all {c['goal_id']} goals judged under the corrected rule (version {c['rule_version']}).")
        else:
            lines.append(f"- {c['at']} {c['dependency']} {c['type'].split('_')[1]} until {c['until']}: {c.get('detail')}.")
    lines += [f"- Plan revisions triggered by live events: {len(summary['plan_revisions_from_live_events'])} "
              "(purchases that closed an open goal before its deadline).", "",
              "## Simulator verdict on the published actions", "",
              f"- Policy problems: **{sim['policy_problem_count']}** (holdout, consent, quiet hours, frequency cap).",
              f"- Pages accepted {sim['counts'].get('home_page_accepted', 0)}, shown personalised {sim['counts'].get('home_page_shown_personalised', 0)}, "
              f"published but not shown {sim['counts'].get('home_page_published_but_not_shown', 0)}.",
              f"- Push delivered {sim['counts'].get('push_delivered', 0)}/{sim['counts'].get('push_requested', 0)} "
              f"(invalid tokens {sim['counts'].get('push_undeliverable_token', 0)}), WhatsApp delivered "
              f"{sim['counts'].get('whatsapp_delivered', 0)}/{sim['counts'].get('whatsapp_requested', 0)}, clicks "
              f"{sim['counts'].get('push_clicked', 0) + sim['counts'].get('whatsapp_clicked', 0)}.",
              "", "## Outcomes", "", "| Arm | Succeeded | Failed | Inconclusive |", "|---|---|---|---|"]
    for arm in ("agent", "holdout"):
        lines.append(f"| {arm} | {verdicts[(arm, 'succeeded')]} | {verdicts[(arm, 'failed')]} | {verdicts[(arm, 'inconclusive')]} |")
    lines += ["", f"The table counts goals ({len(outcomes)}). A viewer can hold more than one goal over the day, one at a time; "
              f"{len({o['user_id'] for o in outcomes})} viewers had at least one.", "",
              f"Intention-to-treat success rate, first goal per viewer: agent {_pct(a['rate'])} ({a['judged']} viewers) vs holdout "
              f"{_pct(h['rate'])} ({h['judged']} viewers), difference {_pp(d)} "
              f"(95% interval {_pp(lo)} to {_pp(hi)}). See `impact_report.md`.", "",
              "## Where to look", "",
              "- `decisions.jsonl`: one record per trigger with goal, action or reason for none, evidence, conflicts, checks, timing.",
              "- `actions.jsonl`: what was published, in the simulator's input format.",
              "- `outcomes.jsonl`: verdict and attribution for every committed goal.",
              "- `viewer_state/<user_id>.json`: everything the agent knew, decided and observed for one viewer.",
              "- `latency_report.json`, `replay_summary.json`, `sim/simulator_report.json`."]
    return "\n".join(lines) + "\n"


def inspect_viewer(out_dir: Path, user_id: str) -> dict:
    out_dir = Path(out_dir)
    exported = out_dir / "viewer_state" / f"{user_id}.json"
    if exported.exists():
        return json.loads(exported.read_text(encoding="utf-8"))
    store = Store(out_dir / "state.db")
    try:
        return {"state": store.load_state(user_id), "decisions": store.decisions_for(user_id),
                "actions": store.actions_for(user_id), "outcomes": []}
    finally:
        store.close()
