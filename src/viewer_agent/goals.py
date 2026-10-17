"""The twelve goals as rules: who is eligible, what counts as success, and how an open goal is judged.

Rules are versioned. goals.json is the first version; a goal_rule_corrected control event produces a new version
that applies to goals that are still open, exactly as the correction says.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .facts import ViewerFacts, event_time
from .pack import Pack
from .times import iso, ist_day, parse_ts

NEW_RELEASE_DAYS = 30
RESUME_GAP_DAYS = 3
ABSENCE_DAYS = 14
HABIT_DAYS_NEEDED = 3


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reason: str
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)  # what the judge needs later, e.g. the series slug for G07


@dataclass
class Verdict:
    verdict: str  # succeeded | failed | inconclusive
    reason: str
    success_event_id: str | None = None
    success_at: str | None = None


class RuleBook:
    def __init__(self, goals_config: dict, settings: Settings, known_events: set[str] | None = None):
        self.goals = {g["goal_id"]: copy.deepcopy(g) for g in goals_config["goals"]}
        self.version = f"goals.json@{goals_config['version']}"
        self.corrections: list[dict] = []
        self.settings = settings
        # event names that actually occur in the stream; a goal whose success event never occurs is not measurable
        self.known_events = known_events

    def apply_correction(self, correction: dict) -> None:
        goal = self.goals[correction["goal_id"]]
        goal["success"] = {**goal["success"], **correction["new_success"]}
        self.corrections.append(correction)
        self.version += f"+{correction['goal_id']}@{correction['at']}"

    def name(self, goal_id: str) -> str:
        return self.goals[goal_id]["name"]

    def window(self, goal_id: str) -> timedelta:
        return timedelta(hours=self.goals[goal_id]["success"]["window_hours"])

    def success_signal(self, goal_id: str) -> str:
        s = self.goals[goal_id]["success"]
        return f"{s['event']} ({s['condition']}) within {s['window_hours']}h"

    def measurable(self, goal_id: str) -> tuple[bool, str]:
        event = self.goals[goal_id]["success"].get("event")
        if not event:
            return False, "success condition names no event"
        if self.known_events is not None and event not in self.known_events:
            return False, f"success event '{event}' never appears in the event stream"
        return True, ""

    # ------------------------------------------------------------------ eligibility
    def eligible(self, facts: ViewerFacts) -> list[Eligibility]:
        """Eligible goals in priority order. Unmeasurable goals are never offered."""
        if not facts.known():
            return []
        order = list(self.settings.goal_priority) + [g for g in self.goals if g not in self.settings.goal_priority]
        out = []
        for goal_id in order:
            ok, _ = self.measurable(goal_id)
            if ok:
                e = self.check(goal_id, facts)
                if e.eligible:
                    out.append(e)
        return out

    def check(self, goal_id: str, facts: ViewerFacts) -> Eligibility:
        now = facts.now
        f = facts.facts
        status = facts.value("subscription_status")
        signup_at = parse_ts(facts.value("signup_at"))
        plays = facts.value("plays") or {}
        depth = facts.value("history_depth", 0)
        no_play_ref = f"events:{facts.user_id}:play_started=0 of {depth}"

        def yes(reason, evidence, assumptions=(), context=None):
            return Eligibility(goal_id, True, reason, list(dict.fromkeys(evidence)), list(assumptions), context or {})

        def no(reason):
            return Eligibility(goal_id, False, reason)

        if goal_id == "G01":
            if signup_at is None:
                return no("signup date unknown")
            if now - signup_at > timedelta(days=7):
                return no("signed up more than 7 days ago")
            if plays.get("count", 0):
                return no("has already started a play")
            return yes("signed up in the last 7 days, no play_started on record",
                       f["signup_at"].evidence + [no_play_ref], ["events can arrive late; 'no play' means none received yet"])
        if goal_id == "G02":
            if (facts.value("completions") or {}).get("count", 0):
                return no("has completed an episode")
            return yes("no episode_completed on record", f["completions"].evidence,
                       ["events can arrive late; 'no completion' means none received yet"])
        if goal_id == "G03":
            if signup_at is None:
                return no("signup date unknown")
            if now - signup_at > timedelta(days=2):
                return no("signed up more than 2 days ago")
            return yes("signed up in the last 2 days", f["signup_at"].evidence)
        if goal_id == "G04":
            if status != "trial_active":
                return no(f"subscription status is {status}, not trial_active")
            return yes("subscription status trial_active", f["subscription_status"].evidence)
        if goal_id == "G05":
            if status != "never":
                return no(f"subscription status is {status}")
            if facts.value("ever_paid"):
                return no("events show a paid plan in the past")
            return yes("never had a paid plan", f["subscription_status"].evidence + f["ever_paid"].evidence)
        if goal_id == "G06":
            if status != "lapsed":
                return no(f"subscription status is {status}, not lapsed")
            lapsed_at = parse_ts(facts.value("lapsed_at"))
            if lapsed_at is None:
                return no("lapsed, but the lapse date is unknown")
            if now - lapsed_at > timedelta(days=self.settings.g06_recent_days):
                return no(f"lapsed more than {self.settings.g06_recent_days} days ago")
            return yes("lapsed recently", f["subscription_status"].evidence + f["lapsed_at"].evidence,
                       [f"'recently lapsed' read as within {self.settings.g06_recent_days} days (not defined in goals.json)"])
        if goal_id == "G07":
            series = [s for s in facts.value("unfinished_series") or []
                      if now - parse_ts(s["last_played_at"]) >= timedelta(days=RESUME_GAP_DAYS)]
            if not series:
                return no("no unfinished series left untouched for 3+ days")
            s = series[0]
            return yes(f"unfinished series {s['slug']} ({s['watched']}/{s['total']} episodes), last played {s['last_played_at'][:10]}",
                       [s["evidence"]], context={"slug": s["slug"], "title_id": s["title_id"]})
        if goal_id == "G08":
            if status != "active":
                return no(f"subscription status is {status}, not active")
            return yes("active subscriber", f["subscription_status"].evidence)
        if goal_id == "G09":
            if status != "active":
                return no(f"subscription status is {status}, not active")
            days = plays.get("days_in_last_14", [])
            if len(days) > 1:
                return no(f"played on {len(days)} days in the last 14")
            return yes(f"active subscriber with plays on {len(days)} day(s) in the last 14",
                       f["subscription_status"].evidence + f["plays"].evidence)
        if goal_id == "G12":
            last = parse_ts(facts.value("last_visit_before_trigger"))
            if last is None:
                return no("no earlier visit on record, so this is not a return after absence")
            if now - last < timedelta(days=ABSENCE_DAYS):
                return no(f"visited {(now - last).days} days ago")
            return yes(f"no visit for {(now - last).days} days before this open", f["last_visit_before_trigger"].evidence)
        ok, why = self.measurable(goal_id)
        return no(why if not ok else "no rule implemented")

    # ------------------------------------------------------------------ judging
    def judge(self, goal: dict, events: list[dict], observed_until: datetime, pack: Pack) -> Verdict:
        """Judge one committed goal from events that happened inside its window, under the rule now in force."""
        goal_id = goal["goal_id"]
        start, deadline = parse_ts(goal["started_at"]), parse_ts(goal["deadline"])
        success = self.goals[goal_id]["success"]
        window = [e for e in events if start < e["_at"] <= deadline]
        hits = [e for e in window if e["event"] == success["event"] and self._matches(goal, e, pack)]

        if goal_id == "G09":
            days = sorted({ist_day(e["_at"]) for e in hits})
            if len(days) >= HABIT_DAYS_NEEDED:
                third = min((e for e in hits if ist_day(e["_at"]) == days[HABIT_DAYS_NEEDED - 1]), key=lambda e: e["_at"])
                return Verdict("succeeded", f"plays on {len(days)} distinct IST days", third["event_id"], iso(third["_at"]))
            hits = []
        if hits:
            first = min(hits, key=lambda e: e["_at"])
            return Verdict("succeeded", f"{success['event']} matched '{success['condition']}'", first["event_id"], iso(first["_at"]))
        if observed_until >= deadline:
            return Verdict("failed", f"no {success['event']} matching '{success['condition']}' before the deadline")
        return Verdict("inconclusive", f"window still open; observed up to {iso(observed_until)}")

    def signal_met(self, goal: dict, event: dict, pack: Pack) -> bool:
        """Does this single live event already satisfy the goal? (Never true for the 3-day habit goal.)"""
        success = self.goals[goal["goal_id"]]["success"]
        if goal["goal_id"] == "G09" or event["event"] != success["event"]:
            return False
        event = {**event, "_at": event_time(event)}
        return parse_ts(goal["started_at"]) < event["_at"] <= parse_ts(goal["deadline"]) and self._matches(goal, event, pack)

    def _matches(self, goal: dict, e: dict, pack: Pack) -> bool:
        goal_id, props = goal["goal_id"], e["properties"]
        start = parse_ts(goal["started_at"])
        if goal_id == "G03":
            return ist_day(e["_at"]) > ist_day(start)
        if goal_id in ("G04", "G05"):
            return props.get("is_trial") is False
        if goal_id == "G07":
            t = pack.title(props.get("title_id"))
            return bool(t and t.slug == goal.get("context", {}).get("slug"))
        if goal_id == "G08":
            t = pack.title(props.get("title_id"))
            return bool(t and (start.date() - t.release_date).days <= NEW_RELEASE_DAYS)
        return True  # G01, G02, G06, G09, G12: any event of the right type
