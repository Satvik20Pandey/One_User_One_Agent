"""Builds sample_run/demo.html: a read-only page over the outputs of a run.

The page contains no decision logic. Everything it shows is read from decisions.jsonl, actions.jsonl,
outcomes.jsonl, viewer_state/ and the reports, so it can never disagree with what the agent actually did.
It is one self-contained file with no external scripts, so it opens offline and sends no data anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

from .judge import _diff_ci, _rate, pilot_reanalysis
from .pack import Pack
from .replay import read_jsonl

TEMPLATE = Path(__file__).with_name("demo_template.html")


def build_demo(pack_dir: Path, out_dir: Path) -> Path:
    pack_dir, out_dir = Path(pack_dir), Path(out_dir)
    summary = json.loads((out_dir / "replay_summary.json").read_text(encoding="utf-8"))
    latency = json.loads((out_dir / "latency_report.json").read_text(encoding="utf-8"))
    sim = json.loads((out_dir / "sim" / "simulator_report.json").read_text(encoding="utf-8"))
    outcomes = read_jsonl(out_dir / "outcomes.jsonl")

    viewers, used_titles = {}, set()
    for path in sorted((out_dir / "viewer_state").glob("*.json")):
        v = json.loads(path.read_text(encoding="utf-8"))
        viewers[path.stem] = _compact_viewer(v, used_titles)

    pack = Pack(pack_dir)
    titles = {tid: [t.name, t.dialect, t.content_type, t.genres] for tid in sorted(used_titles)
              if (t := pack.title(tid))}

    data = {
        "summary": {k: summary[k] for k in ("triggers", "viewers", "decisions_by_status", "actions_by_type",
                                            "goals_chosen", "no_action_reasons", "fallbacks", "model")},
        "controls": summary["control_events"],
        "revisions": summary["plan_revisions_from_live_events"],
        "latency": latency,
        "sim": {"policy_problem_count": sim["policy_problem_count"], "counts": sim["counts"], "seed": sim["seed"]},
        "impact": _impact(outcomes, pack_dir),
        "examples": _examples(viewers, summary),
        "goals": {g["goal_id"]: g["name"] for g in pack.goals_config["goals"]},
        "titles": titles,
        "viewers": viewers,
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    out = out_dir / "demo.html"
    out.write_text(html, encoding="utf-8")
    return out


def _compact_viewer(v: dict, used_titles: set) -> dict:
    """Only what the page shows, so the file stays small enough to open quickly."""
    state = v.get("state") or {}
    decisions = []
    for d in v.get("decisions", []):
        decisions.append({k: d.get(k) for k in (
            "decision_id", "status", "action", "actions", "no_action_reason", "no_goal_reason", "goal", "dialect",
            "confidence", "eligibility", "checks", "fallback_used", "decided_in_ms", "published_at", "assumptions",
            "conflicts", "duplicate_of")} | {
            "trigger": {k: d["trigger"].get(k) for k in ("event", "event_id", "delivered_at", "event_age_s", "live_visit",
                                                         "device_clock_trusted")},
            "model": {k: d["model"].get(k) for k in ("provider", "calls", "used", "rejected", "failure", "note")},
            "budget": d.get("budget"),
            "evidence": [e["ref"] for e in d.get("evidence", [])][:8],
        })
    actions = []
    for a in v.get("actions", []):
        keep = {k: a.get(k) for k in ("action_id", "decision_id", "type", "published_at", "send_at", "dialect",
                                      "hero_title_id", "title_id", "template_id", "variables", "text", "replaces",
                                      "reason", "goal_id")}
        keep["rows"] = [[r["row_type"], r["title_ids"]] for r in a.get("rows") or []]
        used_titles.update(t for r in a.get("rows") or [] for t in r["title_ids"])
        used_titles.update(t for t in (a.get("hero_title_id"), a.get("title_id")) if t)
        actions.append({k: val for k, val in keep.items() if val not in (None, [], {})})
    goals = (state.get("goal_history") or []) + ([state["active_goal"]] if state.get("active_goal") else [])
    raw = v.get("decisions") or [{}]
    return {
        "arm": state.get("arm") or raw[0].get("arm"),
        "facts": {name: [f["value"], f["source"], f["observed_at"], f.get("note", "")]
                  for name, f in (state.get("facts") or {}).items()},
        "conflicts": state.get("conflicts", []),
        "learnings": state.get("learnings", []),
        "goals": [{k: g.get(k) for k in ("goal_id", "name", "started_at", "deadline", "success_signal", "rule_version",
                                         "eligibility_reason", "close_reason", "shadow")} for g in goals],
        "decisions": decisions,
        "actions": actions,
        "outcomes": [{k: o.get(k) for k in ("goal_id", "verdict", "reason", "attribution", "exposure", "success_at",
                                            "deadline", "rule_version_at_judgement")} for o in v.get("outcomes", [])],
        "note": state.get("note"),
    }


def _examples(viewers: dict, summary: dict) -> list[dict]:
    """One viewer per situation the brief cares about, picked from the run itself."""
    revised = {r["user_id"] for r in summary["plan_revisions_from_live_events"]}

    def first(label, why, test):
        for user_id, v in viewers.items():
            if test(user_id, v):
                return {"label": label, "why": why, "user_id": user_id}
        return None

    def has(v, test):
        return any(test(d) for d in v["decisions"])

    picks = [
        first("Acted, then goal reached", "page + message published, viewer saw it, goal succeeded",
              lambda u, v: v["arm"] == "agent" and len(v["actions"]) >= 2
              and any(o["attribution"] == "exposed_before_success" for o in v["outcomes"])),
        first("Holdout viewer", "goal chosen as a shadow, nothing published",
              lambda u, v: v["arm"] == "holdout" and has(v, lambda d: d["status"] == "HOLDOUT_NO_ACTION" and d["goal"])),
        first("Purchase closed the goal", "profile said 'never paid'; a purchase arrived minutes after the decision",
              lambda u, v: u in revised and v["arm"] == "agent"),
        first("Three dialects disagree", "app setting, profile and campaign link all differ",
              lambda u, v: v["arm"] == "agent" and has(v, lambda d: len((d["dialect"] or {}).get("conflicts") or []) >= 2)),
        first("Model made an unsupported claim", "model said 'eligible for G04'; no event supports it",
              lambda u, v: has(v, lambda d: d["model"].get("rejected"))),
        first("Model provider down", "HTTP 503 window: rule-based fallback",
              lambda u, v: has(v, lambda d: "503" in (d["model"].get("failure") or ""))),
        first("Data store degraded", "slow reads charged to the latency budget",
              lambda u, v: has(v, lambda d: (d.get("budget") or {}).get("simulated_wait_ms"))),
        first("Opened offline, days ago", "buffered open is not a live visit: no action",
              lambda u, v: v["arm"] == "agent" and has(v, lambda d: d["trigger"].get("live_visit") is False
                                                       and (d["trigger"].get("event_age_s") or 0) > 86400)),
        first("Same trigger delivered twice", "second copy returns the first decision",
              lambda u, v: has(v, lambda d: d["status"] == "DUPLICATE")),
        first("Dialect with no content", "app set to a dialect the catalogue does not have",
              lambda u, v: has(v, lambda d: "no catalogue content" in (d.get("no_action_reason") or ""))),
        first("Withdrawn title removed", "licence ended mid-day; live page republished without it",
              lambda u, v: any(a.get("replaces") for a in v["actions"])),
        first("Daily message cap respected", "second open, message already scheduled: page only",
              lambda u, v: has(v, lambda d: any("already scheduled" in a for a in d.get("assumptions") or []))),
    ]
    return [p for p in picks if p]


def _impact(outcomes: list[dict], pack_dir: Path) -> dict:
    """Same calculation as impact_report.md (it calls the same functions), in a shape the page can draw."""
    first = {}
    for o in sorted(outcomes, key=lambda o: o["started_at"]):
        first.setdefault(o["user_id"], o)
    rows = list(first.values())
    agent = [o for o in rows if o["arm"] == "agent"]
    holdout = [o for o in rows if o["arm"] == "holdout"]
    a, h = _rate(agent), _rate(holdout)
    d, lo, hi = _diff_ci(a, h)
    by_goal = []
    for goal_id in sorted({o["goal_id"] for o in rows}):
        ga = _rate([o for o in agent if o["goal_id"] == goal_id])
        gh = _rate([o for o in holdout if o["goal_id"] == goal_id])
        by_goal.append({"goal_id": goal_id, "agent": ga, "holdout": gh, "diff": _diff_ci(ga, gh)[0]})
    exposed = _rate([o for o in agent if o["attribution"] in ("exposed_before_success", "exposed_no_success")])
    unexposed = _rate([o for o in agent if o["attribution"] in ("published_but_never_seen", "no_action_taken",
                                                                 "success_before_exposure")])
    pilot = pilot_reanalysis(pack_dir)
    return {"agent": a, "holdout": h, "diff": d, "lo": lo, "hi": hi, "by_goal": by_goal,
            "exposed": exposed, "unexposed": unexposed, "goals_total": len(outcomes), "viewers_with_goal": len(rows),
            "pilot": {k: pilot[k] for k in ("served", "not_served", "treatment", "holdout")} if pilot else None}
