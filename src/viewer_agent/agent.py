"""One agent per viewer.

The agent owns the viewer's goal, the single active plan and the record of everything it did. It is created on
demand, keeps no state of its own between calls (everything durable lives in the store) and handles each trigger
inside a time and call budget.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .facts import ViewerFacts, build_facts
from .goals import RuleBook
from .pack import Pack
from .planner import ModelProvider, Proposal, propose
from .store import Store, StoreTimeout
from .times import iso, ist_day, next_send_slot, parse_ts
from .verifier import verify

SUBSCRIBER_GOALS = {"G08", "G09", "G11"}


class BudgetExceeded(Exception):
    pass


class Budget:
    """Counts model and tool calls and keeps the clock. Waiting on a slow dependency is charged as simulated time
    so the replay can run fast while the latency report stays honest."""

    def __init__(self, now: datetime, settings: Settings):
        self.now = now
        self.settings = settings
        self.model_calls = 0
        self.tool_calls = 0
        self.tools: list[str] = []
        self.simulated_wait_s = 0.0
        self.queued_s = 0.0
        self._start = time.perf_counter()

    def tool(self, name: str) -> None:
        self.tool_calls += 1
        self.tools.append(name)
        if self.tool_calls > self.settings.max_tool_calls:
            raise BudgetExceeded(f"tool call budget of {self.settings.max_tool_calls} exceeded at {name}")

    def model(self) -> None:
        self.model_calls += 1
        if self.model_calls > self.settings.max_model_calls:
            raise BudgetExceeded(f"model call budget of {self.settings.max_model_calls} exceeded")

    def can_call_model(self) -> bool:
        return self.model_calls < self.settings.max_model_calls

    def remaining_s(self) -> float:
        return self.settings.latency_budget_s - self.elapsed_ms() / 1000

    def wait(self, seconds: float) -> None:
        self.simulated_wait_s += seconds

    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._start) * 1000 + self.simulated_wait_s * 1000)

    def as_dict(self) -> dict:
        return {"model_calls": self.model_calls, "tool_calls": self.tool_calls,
                "simulated_wait_ms": int(self.simulated_wait_s * 1000)}


class Ids:
    """Sequential ids. Started after the highest id the store already holds, so a run that continues an earlier
    store never collides (a goal is created by at most one decision, so that is a safe start for goal ids too)."""

    def __init__(self, decisions: int = 0, actions: int = 0):
        self.decisions = decisions
        self.actions = actions
        self.goals = decisions

    @classmethod
    def after(cls, store: Store) -> "Ids":
        return cls(store.highest_number("decisions", "decision_id"), store.highest_number("actions", "action_id"))

    def decision(self) -> str:
        self.decisions += 1
        return f"D-{self.decisions:05d}"

    def action(self) -> str:
        self.actions += 1
        return f"A-{self.actions:05d}"

    def goal(self) -> str:
        self.goals += 1
        return f"G-{self.goals:05d}"


@dataclass
class Deps:
    pack: Pack
    store: Store
    rules: RuleBook
    settings: Settings
    provider: ModelProvider | None = None
    ids: Ids = field(default_factory=Ids)
    shed_model: bool = False  # set by the harness during a traffic surge: skip optional model calls


class ViewerAgent:
    def __init__(self, user_id: str, deps: Deps):
        self.user_id = user_id
        self.d = deps

    # ------------------------------------------------------------------ triggers
    def handle_trigger(self, trigger: dict, now: datetime, queued_s: float = 0.0) -> tuple[dict, list[dict]]:
        """Decide for one app open. Returns the decision record and the actions to publish (possibly none).
        `queued_s` is how long the open waited for a free worker; it counts against the same 60 s budget."""
        s, store, pack = self.d.settings, self.d.store, self.d.pack
        budget = Budget(now, s)
        budget.wait(queued_s)
        budget.queued_s = queued_s

        budget.tool("store:find_decision")
        earlier = store.find_decision(trigger["event_id"])
        if earlier:
            record = self._duplicate(trigger, now, earlier, budget)
            store.save_decision(record)  # kept for the audit trail; the lookup above skips duplicates
            return record, []

        state, events, degraded = self._load(budget)
        budget.tool("pack:arms")
        arm = pack.arms.get(self.user_id)
        state["arm"] = arm
        facts = build_facts(self.user_id, trigger, events, pack, now, s)
        if degraded:
            facts.assumptions.append("user_data_store unavailable; decided from the trigger and the pack only")

        actions: list[dict] = []
        checks = None
        fallback = "store_degraded" if degraded else None
        proposal: Proposal
        goal_for_record, new_goal = None, False
        freshness = facts.value("trigger_freshness")

        if not freshness["live_visit"]:
            age = freshness["event_age_s"]
            proposal = Proposal(None, None, None, f"trigger is {age // 86400}d {age % 86400 // 3600}h {age % 3600 // 60}m old "
                                                  "(buffered offline); the viewer is not in the app now")
        elif degraded:
            # Smallest safe action: a page that needs nothing about the viewer beyond the dialect the app sent.
            proposal = self._dialect_only_proposal(facts, now, budget)
        else:
            try:
                proposal = self._plan(state, facts, arm, now, budget)
            except BudgetExceeded as exc:
                proposal = Proposal(None, None, None, f"budget exceeded before a decision: {exc}")
                fallback = "budget_exceeded"
            if proposal.model.get("failure"):
                fallback = fallback or "model_unavailable"

        if arm == "holdout":
            # Shadow decision: the goal is chosen and recorded so the arms stay comparable, nothing is published.
            goal_for_record, new_goal = self._commit_goal(state, proposal, now)
            status, checks = "HOLDOUT_NO_ACTION", None
            proposal.page, proposal.message = None, None
            proposal.no_action_reason = proposal.no_action_reason or "viewer is in the randomized holdout arm"
        else:
            if proposal.page or proposal.message:
                try:
                    checks = verify(proposal, facts, arm, self.d.rules, store, pack, s, now, budget)
                except BudgetExceeded as exc:
                    checks = None
                    proposal.page = proposal.message = None
                    proposal.no_action_reason = f"budget exceeded during verification: {exc}"
                    fallback = "budget_exceeded"
                if checks:
                    self._apply_vetoes(proposal, checks)
            goal_for_record, new_goal = self._commit_goal(state, proposal, now) if not degraded else (None, False)
            decided_ms = budget.elapsed_ms()
            if decided_ms > s.latency_fail_s * 1000:
                proposal.page = proposal.message = None
                proposal.no_action_reason = f"decision took {decided_ms} ms, over the {s.latency_fail_s}s failure limit"
                status = "FAILED"
            elif proposal.page or proposal.message:
                status = "LATE" if decided_ms > s.latency_budget_s * 1000 else "PUBLISHED"
            else:
                status = "NO_ACTION"

        decided_ms = budget.elapsed_ms()
        published_at = now + timedelta(milliseconds=decided_ms)
        decision_id = self.d.ids.decision()
        if status in ("PUBLISHED", "LATE"):
            actions = self._action_records(decision_id, trigger, proposal, published_at, goal_for_record)

        record = self._decision_record(decision_id, trigger, now, arm, facts, proposal, goal_for_record, new_goal,
                                       checks, budget, fallback, decided_ms, published_at, status, actions)

        state["facts"] = facts.snapshot()
        state["conflicts"] = facts.conflicts
        state["last_session_id"] = trigger.get("session_id")
        state.setdefault("decisions", []).append(decision_id)
        state.setdefault("actions", []).extend(a["action_id"] for a in actions)
        state["updated_at"] = iso(published_at)
        store.save_decision(record)
        for a in actions:
            store.save_action(a)
        if not degraded:
            store.save_state(self.user_id, state, published_at)
        return record, actions

    # ------------------------------------------------------------------ live events
    def handle_event(self, event: dict, now: datetime) -> dict | None:
        """A non-trigger event arrived. Store it and, if it changes the viewer's situation, revise the plan now."""
        store = self.d.store
        if not store.add_event(event):
            return None  # duplicate delivery
        if event["event"] not in ("purchase_completed", "subscription_renewed", "subscription_lapsed",
                                  "notification_opt_out"):
            return None
        if event["event"] == "notification_opt_out":
            cancelled = self._cancel_scheduled(event["properties"].get("channel"), now, "viewer opted out")
            return {"user_id": self.user_id, "event": event["event"],
                    "note": f"opt-out from {event['properties'].get('channel')}; cancelled {len(cancelled)} scheduled message(s)"} if cancelled else None
        state = store.load_state(self.user_id)
        if not state or not state.get("active_goal"):
            return None
        goal = state["active_goal"]
        note = None
        if self.d.rules.signal_met(goal, event, self.d.pack):
            note = f"{event['event']} arrived at {event['received_at']}; success signal for {goal['goal_id']} received, goal closed for judgement"
            self._close_goal(state, now, "success_signal_received", event["event_id"])
        elif event["event"] == "subscription_lapsed" and goal["goal_id"] in SUBSCRIBER_GOALS:
            note = f"subscription lapsed at {event['received_at']}; {goal['goal_id']} no longer applies, goal closed"
            self._close_goal(state, now, "subscription_lapsed", event["event_id"])
        elif event["event"] in ("purchase_completed", "subscription_renewed") and goal["goal_id"] in ("G05", "G06", "G04"):
            note = f"{event['event']} arrived; {goal['goal_id']} is moot, goal closed for judgement"
            self._close_goal(state, now, "state_changed:" + event["event"], event["event_id"])
        if note:
            cancelled = self._cancel_scheduled(None, now, f"goal {goal['goal_id']} closed: {event['event']}",
                                               goal_id=goal["goal_id"])
            if cancelled:
                note += f"; cancelled scheduled message(s) {', '.join(cancelled)}"
            state.setdefault("learnings", []).append(f"{iso(now)} {note}")
            state["replan_reason"] = note
            store.save_state(self.user_id, state, now)
            return {"user_id": self.user_id, "event": event["event"], "note": note}
        return None

    # ------------------------------------------------------------------ helpers
    def _plan(self, state: dict, facts: ViewerFacts, arm: str | None, now: datetime, budget: Budget) -> Proposal:
        """Goal first, then what to change. Every step here counts against the tool and model budget."""
        keep = self._reconcile_goal(state, facts, now)
        learned = self._learned_skips(state, now)
        budget.tool("rules:eligible")
        eligible = self.d.rules.eligible(facts, skip=learned)
        live_page = self._live_page(facts, now, budget) if arm == "agent" else None
        if live_page:
            goal = next((e for e in eligible if e.goal_id == keep), None) or (eligible[0] if eligible else None)
            return Proposal(goal, None, None, f"home page {live_page['action_id']} for this viewer is still live "
                                              f"(published {live_page['published_at']}); nothing to change")
        # The model is only consulted when its advice can be used: never for holdout shadow decisions.
        confidence, why = facts.confidence()
        if arm == "agent" and confidence < self.d.settings.min_confidence_to_act:
            goal = next((e for e in eligible if e.goal_id == keep), None) or (eligible[0] if eligible else None)
            return Proposal(goal, None, None, f"not enough evidence to act (confidence {confidence:.2f}: "
                                              f"{'; '.join(why)}); nothing published")
        provider = self.d.provider if arm == "agent" and not self.d.shed_model else None
        message_block = self._message_block(now, budget) if arm == "agent" else None
        proposal = propose(facts, eligible, self.d.pack, self.d.settings, now, provider, budget, keep_goal=keep,
                           message_block=message_block)
        if proposal.message and proposal.message.channel == "in_app_message" and self.d.store.messages_on_day(
                self.user_id, ist_day(now), types=("in_app_message",)):
            proposal.message = None
            proposal.assumptions.append("an in-app message was already shown today; page only")
        proposal.assumptions += [f"learned: {why}" for why in learned.values()]
        if arm == "agent" and self.d.shed_model:
            proposal.model["skipped"] = "traffic surge: optional model call skipped to keep decisions fast"
        return proposal

    def _learned_skips(self, state: dict, now: datetime) -> dict[str, str]:
        """Goals that already failed for this viewer, after the agent had acted, are not retried for a while.
        This is where judged outcomes feed back into the next plan."""
        skip = {}
        for goal in state.get("goal_history", []):
            outcome = goal.get("outcome") or {}
            ended = parse_ts(goal["deadline"])
            if outcome.get("verdict") == "failed" and now - ended < timedelta(days=self.d.settings.retry_failed_goal_days):
                skip[goal["goal_id"]] = (f"{goal['goal_id']} failed on {goal['deadline'][:10]} "
                                         f"({outcome.get('attribution', 'unknown').replace('_', ' ')}); "
                                         f"not retried for {self.d.settings.retry_failed_goal_days} days")
        return skip

    def _cancel_scheduled(self, channel: str | None, now: datetime, reason: str, goal_id: str | None = None) -> list[str]:
        """Scheduled push/WhatsApp messages that have not gone out yet can still be withdrawn: all of one channel
        (after an opt-out) or all of one goal (after the goal closed)."""
        cancelled = []
        for a in self.d.store.actions_for(self.user_id):
            wanted = a["type"] == channel if channel else (a["type"] in ("push", "whatsapp") and a.get("goal_id") == goal_id)
            if wanted and not a.get("cancelled_at") and parse_ts(a["send_at"]) > now:
                self.d.store.cancel_action(a, now, reason)
                cancelled.append(a["action_id"])
        return cancelled

    def _load(self, budget: Budget) -> tuple[dict, list[dict], bool]:
        store = self.d.store
        for attempt in (1, 2):
            try:
                budget.tool("store:load_state")
                state = store.load_state(self.user_id, budget) or self._new_state()
                budget.tool("store:events_for")
                events = store.events_for(self.user_id, budget)
                return state, events, False
            except StoreTimeout:
                continue
        return self._new_state(), [], True

    def _new_state(self) -> dict:
        return {"user_id": self.user_id, "active_goal": None, "goal_history": [], "learnings": []}

    def _live_page(self, facts: ViewerFacts, now: datetime, budget: Budget) -> dict | None:
        """A page we published less than TTL ago, in the dialect the app is set to now, is still being served."""
        dialect = (facts.value("dialect") or {}).get("page_dialect")
        if not dialect:
            return None
        budget.tool("store:live_pages")
        pages = [p for p in self.d.store.live_pages(now, self.d.settings.home_page_ttl_min, self.user_id)
                 if p["dialect"] == dialect]
        return pages[-1] if pages else None

    def _message_block(self, now: datetime, budget: Budget) -> str | None:
        """Reason not to plan a message at all, or None. One push or WhatsApp per viewer per IST day."""
        send_at = next_send_slot(now, self.d.settings.message_send_hour_ist)
        budget.tool("store:messages_on_day")
        if self.d.store.messages_on_day(self.user_id, ist_day(send_at)):
            return f"a message is already scheduled for {ist_day(send_at)}; page only"
        return None

    def _reconcile_goal(self, state: dict, facts: ViewerFacts, now: datetime) -> str | None:
        """Keep the active goal while it is open and still applies; otherwise close it and let a new one be chosen."""
        goal = state.get("active_goal")
        if not goal:
            return None
        if parse_ts(goal["deadline"]) <= now:
            self._close_goal(state, now, "window_ended")
            return None
        again = self.d.rules.check(goal["goal_id"], facts)
        if not again.eligible:
            self._close_goal(state, now, "no_longer_eligible:" + again.reason)
            state.setdefault("learnings", []).append(f"{iso(now)} {goal['goal_id']} closed early: {again.reason}")
            return None
        return goal["goal_id"]

    def _close_goal(self, state: dict, now: datetime, reason: str, event_id: str | None = None) -> None:
        goal = state.get("active_goal")
        if goal:
            goal.update({"closed_at": iso(now), "close_reason": reason, "close_event_id": event_id})
            state.setdefault("goal_history", []).append(goal)
            state["active_goal"] = None

    def _commit_goal(self, state: dict, proposal: Proposal, now: datetime) -> tuple[dict | None, bool]:
        if proposal.goal is None:
            return state.get("active_goal"), False
        active = state.get("active_goal")
        if active and active["goal_id"] == proposal.goal.goal_id:
            return active, False
        rules = self.d.rules
        goal_id = proposal.goal.goal_id
        goal = {"instance_id": self.d.ids.goal(), "goal_id": goal_id, "name": rules.name(goal_id),
                "started_at": iso(now), "deadline": iso(now + rules.window(goal_id)),
                "success_signal": rules.success_signal(goal_id), "rule_version": rules.version,
                "eligibility_reason": proposal.goal.reason, "evidence": proposal.goal.evidence,
                "context": proposal.goal.context, "shadow": state.get("arm") == "holdout"}
        state["active_goal"] = goal
        return goal, True

    def _dialect_only_proposal(self, facts: ViewerFacts, now: datetime, budget: Budget) -> Proposal:
        from .content import build_page
        dialect = (facts.value("dialect") or {}).get("page_dialect")
        if not dialect:
            return Proposal(None, None, None, "user_data_store unavailable and app dialect unknown; nothing safe to publish")
        budget.tool("catalogue:page")
        page = build_page("G02", facts, self.d.pack, now, self.d.settings)  # generic rows, no viewer history involved
        if page is None:
            return Proposal(None, None, None, "user_data_store unavailable and no page possible for this dialect")
        return Proposal(None, page, None, None, assumptions=["generic dialect page published without a goal"])

    @staticmethod
    def _apply_vetoes(proposal: Proposal, checks) -> None:
        reasons = [f"{f['check']}: {f['reason']}" for f in checks.failed]
        if checks.blocked("page"):
            proposal.page = None
        if checks.blocked("message"):
            proposal.message = None
        if proposal.page is None and proposal.message is None and reasons:
            proposal.no_action_reason = "verifier vetoed the action: " + " | ".join(reasons)
        elif reasons:
            proposal.assumptions.append("verifier dropped part of the plan: " + " | ".join(reasons))

    def _action_records(self, decision_id: str, trigger: dict, proposal: Proposal, published_at: datetime,
                        goal: dict | None) -> list[dict]:
        out = []
        base = {"decision_id": decision_id, "user_id": self.user_id, "trigger_event_id": trigger["event_id"],
                "published_at": iso(published_at), "goal_id": goal["goal_id"] if goal else None}
        if proposal.page:
            p = proposal.page
            out.append({"action_id": self.d.ids.action(), **base, "type": "home_page", "dialect": p.dialect,
                        "hero_title_id": p.hero_title_id,
                        "rows": [{"row_type": r.row_type, "title_ids": r.title_ids} for r in p.rows]})
        if proposal.message:
            m = proposal.message
            rec = {"action_id": self.d.ids.action(), **base, "type": m.channel, "send_at": iso(m.send_at),
                   "title_id": m.title_id, "dialect": m.dialect}
            if m.channel == "whatsapp":
                rec.update({"template_id": m.template_id, "variables": m.variables})
            else:
                rec.update({"text": m.text, "deeplink": m.deeplink})
            out.append(rec)
        return out

    def _decision_record(self, decision_id, trigger, now, arm, facts: ViewerFacts, proposal: Proposal, goal, new_goal,
                         checks, budget: Budget, fallback, decided_ms, published_at, status, actions) -> dict:
        freshness = facts.value("trigger_freshness")
        dialect = facts.value("dialect") or {}
        confidence = self._confidence(facts, proposal, fallback, checks)
        action_names = [a["type"].upper() for a in actions]
        if goal is None:
            no_goal_reason = proposal.no_action_reason if proposal.goal is None else None
            if fallback == "store_degraded":
                no_goal_reason = "user_data_store unavailable; no goal chosen without the viewer's history"
        else:
            no_goal_reason = None
        content = []
        for a in actions:
            if a["type"] == "home_page":
                content += [f"title:{a['hero_title_id']}"] + [f"title:{t}" for r in a["rows"] for t in r["title_ids"]]
            else:
                content.append(f"title:{a['title_id']}")
        content = list(dict.fromkeys(content))
        return {
            "decision_id": decision_id,
            "viewer_id": self.user_id,
            "arm": arm,
            "trigger": {"event": trigger["event"], "event_id": trigger["event_id"], "session_id": trigger.get("session_id"),
                        "received_at": trigger["received_at"], "delivered_at": iso(now),
                        "event_age_s": freshness["event_age_s"], "live_visit": freshness["live_visit"],
                        "device_clock_trusted": freshness["device_clock_trusted"]},
            "goal": ({"id": goal["goal_id"], "name": goal["name"], "instance_id": goal["instance_id"],
                      "started_at": goal["started_at"], "deadline": goal["deadline"],
                      "success_signal": goal["success_signal"], "rule_version": goal["rule_version"],
                      "committed_now": new_goal, "shadow": goal.get("shadow", False)} if goal else None),
            "no_goal_reason": no_goal_reason,
            "action": action_names[0] if action_names else "NO_ACTION",
            "actions": action_names,
            "no_action_reason": None if action_names else proposal.no_action_reason,
            "dialect": {"used": dialect.get("used"), "source": dialect.get("source"), "conflicts": dialect.get("conflicts", [])},
            "content": content,
            "confidence": confidence,
            "evidence": facts.evidence(),
            # what the agent knew at this moment, so any past decision can be audited without replaying it
            "facts": {name: {"value": f.value, "source": f.source, "observed_at": f.observed_at}
                      for name, f in facts.facts.items()},
            "assumptions": facts.assumptions + (proposal.goal.assumptions if proposal.goal else []) + proposal.assumptions,
            "conflicts": facts.conflicts,
            "eligibility": {"goal": proposal.goal.goal_id, "reason": proposal.goal.reason,
                            "evidence": proposal.goal.evidence} if proposal.goal else None,
            "model": proposal.model,
            "checks": checks.as_dict() if checks else {"passed": [], "failed": []},
            "budget": budget.as_dict(),
            "fallback_used": fallback,
            "queue_wait_ms": int(round(budget.queued_s * 1000)),
            "decided_in_ms": decided_ms,
            "published_at": iso(published_at),
            "status": status,
        }

    @staticmethod
    def _confidence(facts: ViewerFacts, proposal: Proposal, fallback, checks) -> dict:
        """Evidence confidence (see ViewerFacts.confidence), lowered when the agent had to fall back or was vetoed."""
        score, why = facts.confidence()
        if fallback:
            score -= 0.2
            why.append(f"fallback: {fallback}")
        if checks and checks.failed:
            score -= 0.1
            why.append("verifier dropped part of the plan")
        return {"value": round(min(0.9, max(0.05, score)), 2), "reason": "; ".join(why)}

    def _duplicate(self, trigger: dict, now: datetime, earlier: dict, budget: Budget) -> dict:
        return {
            "decision_id": self.d.ids.decision(), "viewer_id": self.user_id, "arm": earlier.get("arm"),
            "trigger": {"event": trigger["event"], "event_id": trigger["event_id"], "session_id": trigger.get("session_id"),
                        "received_at": trigger["received_at"], "delivered_at": iso(now)},
            "goal": {**earlier["goal"], "committed_now": False} if earlier.get("goal") else None,
            "action": "NO_ACTION", "actions": [],
            "no_action_reason": f"duplicate delivery of trigger {trigger['event_id']}; already decided in {earlier['decision_id']}",
            "duplicate_of": earlier["decision_id"], "dialect": earlier.get("dialect"), "content": [],
            "confidence": {"value": 1.0, "reason": "same event_id already processed"},
            "evidence": [{"ref": f"decision:{earlier['decision_id']}", "source": "store", "observed_at": earlier["published_at"]}],
            "assumptions": [], "conflicts": [], "model": {"provider": None, "calls": 0, "used": False, "rejected": None},
            "checks": {"passed": ["idempotency"], "failed": []}, "budget": budget.as_dict(), "fallback_used": None,
            "decided_in_ms": budget.elapsed_ms(), "published_at": iso(now), "status": "DUPLICATE",
        }
