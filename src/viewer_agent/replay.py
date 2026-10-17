"""Replay harness.

Feeds the replay day to the agents in delivery order: app-open triggers, the other live events, and the control
events (title withdrawn, rule corrected, dependency outage). Runs faster than real time; each decision's own
processing time (plus any simulated waiting on a slow dependency) is measured and becomes its publish delay.
"""
from __future__ import annotations

import heapq
import json
import os
import sys
from collections import Counter, deque
from datetime import timedelta
from datetime import datetime
from pathlib import Path

from .agent import Deps, Ids, ViewerAgent
from .config import Settings
from .goals import RuleBook
from .pack import Pack
from .planner import MockProvider, OpenAIProvider
from .store import Store
from .times import IST, iso, parse_ts


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows) -> int:
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
    return n


def make_provider(name: str, faults: list[dict], settings: Settings):
    if name == "mock":
        return MockProvider(faults, latency_s=settings.mock_model_latency_s)
    if name == "openai":
        return OpenAIProvider(model=os.environ.get("VIEWER_AGENT_MODEL", "gpt-4o-mini"),
                              base_url=os.environ.get("OPENAI_BASE_URL"), api_key=os.environ.get("OPENAI_API_KEY"))
    raise ValueError(f"unknown provider {name!r}")


class Replay:
    def __init__(self, pack_dir: Path, out_dir: Path, provider_name: str = "mock", settings: Settings | None = None):
        self.pack_dir, self.out_dir = Path(pack_dir), Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings or Settings()
        db = self.out_dir / "state.db"
        for p in (db, Path(str(db) + "-journal")):
            if p.exists():
                p.unlink()
        self.pack = Pack(self.pack_dir)
        self.store = Store(db)
        self.history = self.store.ingest_history(self.pack_dir / "data" / "events.jsonl")
        self.pack.popularity = self.history["plays_per_title"]
        self.rules = RuleBook(self.pack.goals_config, self.settings, known_events=self.history["event_names"])
        self.provider = make_provider(provider_name, read_jsonl(self.pack_dir / "replay" / "model_faults.jsonl"), self.settings)
        self.deps = Deps(self.pack, self.store, self.rules, self.settings, self.provider, ids=Ids.after(self.store))
        self.surge = SurgeMonitor(self.settings)
        self.agents: dict[str, ViewerAgent] = {}
        self.decisions: list[dict] = []
        self.event_notes: list[dict] = []
        self.control_log: list[dict] = []

    def agent(self, user_id: str) -> ViewerAgent:
        if user_id not in self.agents:
            self.agents[user_id] = ViewerAgent(user_id, self.deps)
        return self.agents[user_id]

    # ------------------------------------------------------------------ main loop
    def run(self, limit: int | None = None) -> dict:
        replay = self.pack_dir / "replay"
        timeline = [("trigger", parse_ts(t["deliver_at"]), t) for t in read_jsonl(replay / "triggers.jsonl")]
        timeline += [("event", parse_ts(e["deliver_at"]), e) for e in read_jsonl(replay / "live_events.jsonl")]
        timeline += [("control", parse_ts(c["at"]), c) for c in read_jsonl(replay / "control_events.jsonl")]
        order = {"control": 0, "event": 1, "trigger": 2}
        timeline.sort(key=lambda x: (x[1], order[x[0]]))

        triggers_seen = 0
        free_at = [0.0] * self.settings.workers  # when each decision worker is next free (epoch seconds)
        for kind, at, item in timeline:
            if kind == "trigger":
                if limit and triggers_seen >= limit:
                    break
                triggers_seen += 1
                self.deps.shed_model = self.surge.add(at)
                # one shared queue: the open waits for the first free worker, and that wait counts against its budget
                start = max(at.timestamp(), heapq.heappop(free_at))
                queued_s = start - at.timestamp()
                self.store.add_event(item)  # the trigger is itself an event the viewer's history must contain
                record, _ = self.agent(item["user_id"]).handle_trigger(item, at, queued_s=queued_s)
                record["during_surge"] = self.deps.shed_model
                heapq.heappush(free_at, start + record["decided_in_ms"] / 1000 - queued_s)
                self.decisions.append(record)
                if triggers_seen % 500 == 0:
                    print(f"  {triggers_seen} triggers processed", file=sys.stderr)
            elif kind == "event":
                note = self.agent(item["user_id"]).handle_event(item, at)
                if note:
                    self.event_notes.append({"at": iso(at), **note})
            else:
                self.control_log.append(self.handle_control(item, at))
        return self.write_outputs()

    # ------------------------------------------------------------------ control events
    def handle_control(self, c: dict, at: datetime) -> dict:
        """Notice a changed condition, work out which viewers and decisions it touches, respond to those only."""
        log = {"at": iso(at), "type": c["type"]}
        if c["type"] == "title_withdrawn":
            self.pack.withdraw(c["title_id"], at)
            touched = []
            for page in self.store.live_pages(at, self.settings.home_page_ttl_min):
                ids = [page.get("hero_title_id")] + [t for r in page["rows"] for t in r["title_ids"]]
                if c["title_id"] in ids:
                    touched.append(self.republish_without(page, c["title_id"], at))
            cancelled = self.cancel_messages_for(c["title_id"], at, c["reason"])
            log.update({"title_id": c["title_id"], "live_pages_republished": touched, "messages_cancelled": cancelled})
        elif c["type"] == "goal_rule_corrected":
            self.rules.apply_correction(c)
            affected = self.store.viewers_with_active_goal(c["goal_id"])
            for user_id in affected:
                state = self.store.load_state(user_id)
                state["active_goal"]["rule_version_now"] = self.rules.version
                state.setdefault("learnings", []).append(
                    f"{iso(at)} success rule for {c['goal_id']} corrected while the goal was open; judged under the new rule")
                self.store.save_state(user_id, state, at)
            log.update({"goal_id": c["goal_id"], "rule_version": self.rules.version,
                        "open_goals_affected": len(affected), "viewers": affected})
        elif c["type"] in ("dependency_unavailable", "dependency_degraded"):
            until = parse_ts(c["until"])
            unavailable = c["type"] == "dependency_unavailable"
            if c["dependency"] == "model_provider" and hasattr(self.provider, "set_outage"):
                self.provider.set_outage(at, until)
            elif c["dependency"] == "user_data_store":
                s = self.settings
                self.store.degrade(until, timeout_s=s.store_degraded_timeout_s,
                                   timeout_rate=1.0 if unavailable else s.store_degraded_timeout_rate,
                                   slow_s=s.store_degraded_slow_s)
            log.update({"dependency": c["dependency"], "until": c["until"], "detail": c.get("detail")})
        return log

    def republish_without(self, page: dict, title_id: str, at: datetime) -> str:
        rows = [{"row_type": r["row_type"], "title_ids": [t for t in r["title_ids"] if t != title_id]} for r in page["rows"]]
        rows = [r for r in rows if len(r["title_ids"]) >= 3]
        hero = None if page.get("hero_title_id") == title_id else page.get("hero_title_id")
        new = {**page, "action_id": self.deps.ids.action(), "published_at": iso(at), "hero_title_id": hero,
               "rows": rows, "replaces": page["action_id"], "reason": f"{title_id} withdrawn"}
        self.store.save_action(new)
        return new["action_id"]

    def cancel_messages_for(self, title_id: str, at: datetime, reason: str) -> list[str]:
        cancelled = []
        for user_id, _ in list(self.store.all_states()):
            for a in self.store.actions_for(user_id):
                if a["type"] in ("push", "whatsapp") and a.get("title_id") == title_id and not a.get("cancelled_at") \
                        and parse_ts(a["send_at"]) > at:
                    self.store.cancel_action(a, at, reason)
                    cancelled.append(a["action_id"])
        return cancelled

    # ------------------------------------------------------------------ outputs
    def write_outputs(self) -> dict:
        out = self.out_dir
        write_jsonl(out / "decisions.jsonl", self.decisions)
        actions = []
        for user_id, _ in self.store.all_states():
            actions += [a for a in self.store.actions_for(user_id) if not a.get("cancelled_at")]
        actions.sort(key=lambda a: (a["published_at"], a["action_id"]))
        write_jsonl(out / "actions.jsonl", actions)

        latency = self.latency_report()
        (out / "latency_report.json").write_text(json.dumps(latency, indent=2), encoding="utf-8")

        summary = {
            "replay_day_ist": _main_day(self.decisions),
            "triggers": len(self.decisions),
            "viewers": len({d["viewer_id"] for d in self.decisions}),
            "decisions_by_status": dict(Counter(d["status"] for d in self.decisions)),
            "actions_by_type": dict(Counter(a["type"] for a in actions)),
            "goals_chosen": dict(Counter(d["goal"]["id"] for d in self.decisions if d.get("goal") and d["goal"].get("committed_now"))),
            "no_action_reasons": dict(Counter(_reason_key(d) for d in self.decisions if d["action"] == "NO_ACTION")),
            "fallbacks": dict(Counter(d["fallback_used"] for d in self.decisions if d.get("fallback_used"))),
            "model": {"calls": sum(d["model"].get("calls", 0) for d in self.decisions),
                      "advice_used": sum(1 for d in self.decisions if d["model"].get("used")),
                      "advice_rejected": sum(1 for d in self.decisions if d["model"].get("rejected")),
                      "failures": sum(1 for d in self.decisions if d["model"].get("failure")),
                      "skipped_not_needed": sum(1 for d in self.decisions if (d["model"].get("skipped") or "").startswith("not needed")),
                      "skipped_surge": sum(1 for d in self.decisions if (d["model"].get("skipped") or "").startswith("traffic surge")),
                      "skipped_time": sum(1 for d in self.decisions if "time budget left" in (d["model"].get("skipped") or ""))},
            "history_ingest": self.history["counts"],
            "catalogue_text_flagged": [{"title_id": tid, "excerpt": text[:140]} for tid, text in sorted(self.pack.flagged_text.items())],
            "traffic_surges": self.surge.windows,
            "unmeasurable_goals": {g: self.rules.measurable(g)[1] for g in self.rules.goals if not self.rules.measurable(g)[0]},
            "control_events": self.control_log,
            "plan_revisions_from_live_events": self.event_notes,
            "latency": {k: latency[k] for k in ("p50_ms", "p95_ms", "max_ms", "within_budget", "late", "failed")},
        }
        (out / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        self.store.close()
        return summary

    def latency_report(self) -> dict:
        times = sorted(d["decided_in_ms"] for d in self.decisions)
        per_minute = Counter(d["trigger"]["delivered_at"][:16] for d in self.decisions)
        budget_ms = self.settings.latency_budget_s * 1000

        def pct(p):
            return times[min(len(times) - 1, int(len(times) * p))] if times else 0

        return {
            "decisions": len(times),
            "budget_s": self.settings.latency_budget_s,
            "failure_threshold_s": self.settings.latency_fail_s,
            "p50_ms": pct(0.5), "p95_ms": pct(0.95), "p99_ms": pct(0.99), "max_ms": times[-1] if times else 0,
            "within_budget": sum(1 for t in times if t <= budget_ms),
            "late": sum(1 for d in self.decisions if d["status"] == "LATE"),
            "failed": sum(1 for d in self.decisions if d["status"] == "FAILED"),
            "with_simulated_wait": sum(1 for d in self.decisions if d["budget"].get("simulated_wait_ms")),
            "workers": self.settings.workers,
            "queue_wait_p95_ms": sorted(d.get("queue_wait_ms", 0) for d in self.decisions)[int(len(times) * 0.95)] if times else 0,
            "queue_wait_max_ms": max((d.get("queue_wait_ms", 0) for d in self.decisions), default=0),
            "decisions_during_surge": sum(1 for d in self.decisions if d.get("during_surge")),
            "max_simulated_wait_ms": max((d["budget"].get("simulated_wait_ms", 0) for d in self.decisions), default=0),
            "peak_triggers_per_minute": max(per_minute.values()) if per_minute else 0,
            "peak_minute_utc": max(per_minute, key=per_minute.get) if per_minute else None,
            "note": "decided_in_ms = measured processing time + simulated waiting on degraded dependencies; "
                    "published_at = deliver_at + decided_in_ms",
        }


def _main_day(decisions: list[dict]) -> str | None:
    """The IST calendar day most triggers were delivered on."""
    days = Counter(parse_ts(d["trigger"]["delivered_at"]).astimezone(IST).date().isoformat() for d in decisions)
    return days.most_common(1)[0][0] if days else None


class SurgeMonitor:
    """Notices traffic surges: a 10-minute window with `surge_ratio` times the opens of the hour before it."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.recent: deque = deque()
        self.windows: list[dict] = []
        self.active = False
        self.first_seen = None

    def add(self, at) -> bool:
        self.first_seen = self.first_seen or at
        self.recent.append(at)
        while self.recent and self.recent[0] < at - timedelta(minutes=70):
            self.recent.popleft()
        last_10 = sum(1 for t in self.recent if t > at - timedelta(minutes=10))
        baseline = (len(self.recent) - last_10) / 6  # opens per 10 minutes over the hour before
        warmed_up = at - self.first_seen >= timedelta(minutes=60)  # a surge only means something against a baseline
        surge = warmed_up and last_10 >= self.s.surge_min_opens and last_10 >= self.s.surge_ratio * max(baseline, 1.0)
        if surge and not self.active:
            last = self.windows[-1] if self.windows else None
            if last and at - parse_ts(last["end"]) <= timedelta(minutes=5):
                pass  # the same surge dipped for a moment; keep one window
            else:
                self.windows.append({"start": iso(at), "end": iso(at), "opens_in_10_min": last_10,
                                     "baseline_per_10_min": round(baseline, 1)})
        if surge:
            w = self.windows[-1]
            w["end"], w["opens_in_10_min"] = iso(at), max(w["opens_in_10_min"], last_10)
        self.active = surge
        return surge


def _reason_key(d: dict) -> str:
    reason = d.get("no_action_reason") or ""
    for key in ("holdout", "duplicate", "buffered offline", "still live", "no catalogue content", "dialect unknown",
                "no evidence", "no measurable goal", "vetoed", "user_data_store", "budget"):
        if key in reason:
            return key
    return reason[:60] or "unspecified"


def run_replay(pack_dir: Path, out_dir: Path, provider_name: str = "mock", limit: int | None = None) -> dict:
    return Replay(pack_dir, out_dir, provider_name).run(limit=limit)
