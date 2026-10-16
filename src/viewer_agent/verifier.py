"""Independent checks that run after the planner and can veto it.

Everything is re-derived from the store, the pack and the policy. Nothing the planner or the model produced is
trusted: eligibility is recomputed from events, consent from the snapshot plus later opt-outs, and so on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .config import Settings
from .facts import ViewerFacts
from .goals import RuleBook
from .pack import Pack
from .planner import Proposal
from .store import Store
from .times import in_quiet_hours, ist_day

FORBIDDEN_DEFAULT = ("last chance", "hurry", "only today", "free forever", "guaranteed")
PRICE_PATTERN = re.compile(r"(?:rs\.?|inr|₹)\s*(\d+)|(\d+)\s*(?:rs|inr|rupees)", re.I)
INJECTION_PATTERN = re.compile(
    r"ignore (?:your |all |the )?(?:previous|prior|above)|system note|\[assistant instruction\]|every user|all users|"
    r"including users who opted out|pre-approved", re.I)


@dataclass
class CheckResult:
    passed: list[str] = field(default_factory=list)
    failed: list[dict] = field(default_factory=list)  # {"check", "reason", "scope": page | message | all}

    def ok(self, name: str) -> None:
        self.passed.append(name)

    def fail(self, name: str, reason: str, scope: str) -> None:
        self.failed.append({"check": name, "reason": reason, "scope": scope})

    def blocked(self, scope: str) -> bool:
        return any(f["scope"] in (scope, "all") for f in self.failed)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "failed": self.failed}


def verify(proposal: Proposal, facts: ViewerFacts, arm: str | None, rules: RuleBook, store: Store, pack: Pack,
           settings: Settings, now: datetime, budget) -> CheckResult:
    r = CheckResult()
    page, msg = proposal.page, proposal.message

    if arm != "agent":
        r.fail("holdout", f"viewer arm is {arm!r}; holdout viewers receive no agent action", "all")
    else:
        r.ok("holdout")

    if proposal.goal is not None:
        again = rules.check(proposal.goal.goal_id, facts)  # recomputed from the facts, not copied from the planner
        measurable, why = rules.measurable(proposal.goal.goal_id)
        if not again.eligible:
            r.fail("eligibility", f"{proposal.goal.goal_id}: {again.reason}", "all")
        elif not measurable:
            r.fail("eligibility", f"{proposal.goal.goal_id}: {why}", "all")
        else:
            r.ok("eligibility")

    if page is not None:
        _check_page(r, page, facts, store, pack, settings, now, budget)
    if msg is not None:
        _check_message(r, msg, facts, store, pack, settings, now, budget)
    if proposal.model.get("used"):
        _check_injection(r, proposal)
    return r


def _check_page(r: CheckResult, page, facts: ViewerFacts, store: Store, pack: Pack, settings: Settings,
                now: datetime, budget) -> None:
    app_dialect = (facts.value("dialect") or {}).get("page_dialect")
    ids = page.title_ids()
    wrong = [t for t in ids if not pack.title(t) or pack.title(t).dialect != page.dialect]
    if page.dialect != app_dialect:
        r.fail("dialect", f"page is {page.dialect} but the app will request {app_dialect}", "page")
    elif wrong:
        r.fail("dialect", f"titles not in page dialect {page.dialect}: {wrong}", "page")
    else:
        r.ok("dialect")

    unavailable = [t for t in ids if not pack.available(t, now)]
    if unavailable:
        r.fail("availability", f"not available at publish time: {unavailable}", "page")
    else:
        r.ok("availability")

    problems = []
    slugs = [pack.title(t).slug for t in ids if pack.title(t)]
    if len(page.rows) > 6:
        problems.append("more than 6 rows")
    for row in page.rows:
        if not 3 <= len(row.title_ids) <= 10:
            problems.append(f"row {row.row_type} has {len(row.title_ids)} titles")
    if len(slugs) != len(set(slugs)):
        problems.append("same content appears twice")
    if problems:
        r.fail("page_rules", "; ".join(problems), "page")
    else:
        r.ok("page_rules")

    budget.tool("store:live_pages")
    live = store.live_pages(now, settings.home_page_ttl_min, facts.user_id)
    if any(p["dialect"] == page.dialect for p in live):
        r.fail("duplicate_action", f"a {page.dialect} page for this viewer is still live (TTL {settings.home_page_ttl_min} min)", "page")
    else:
        r.ok("duplicate_action")


def _check_message(r: CheckResult, msg, facts: ViewerFacts, store: Store, pack: Pack, settings: Settings,
                   now: datetime, budget) -> None:
    channel = msg.channel
    consent = facts.value(f"consent_{channel}")
    if consent is not True:
        r.fail("consent", f"{channel} consent is {consent} ({facts.facts[f'consent_{channel}'].source})", "message")
    else:
        r.ok("consent")

    if in_quiet_hours(msg.send_at, settings.quiet_start_hour_ist, settings.quiet_end_hour_ist):
        r.fail("quiet_hours", f"send time {msg.send_at.isoformat()} is inside 22:00-08:00 IST", "message")
    else:
        r.ok("quiet_hours")

    budget.tool("store:messages_on_day")
    if store.messages_on_day(facts.user_id, ist_day(msg.send_at)):
        r.fail("frequency_cap", "a push or WhatsApp message is already scheduled for that IST day", "message")
    else:
        r.ok("frequency_cap")

    title = pack.title(msg.title_id)
    if title is None or title.dialect != msg.dialect:
        r.fail("message_dialect", f"title {msg.title_id} is not in dialect {msg.dialect}", "message")
    elif not title.available_at(msg.send_at):
        r.fail("message_availability", f"title {msg.title_id} not available at send time", "message")
    else:
        r.ok("message_content")

    copy_rules = pack.interventions["push"]["copy"]
    problems = []
    if channel == "whatsapp":
        templates = {t["template_id"]: t for t in pack.interventions["whatsapp"]["approved_templates"]}
        t = templates.get(msg.template_id)
        if t is None:
            r.fail("template", f"{msg.template_id} is not an approved template", "message")
        elif set(t["variables"]) != set((msg.variables or {}).keys()):
            r.fail("template", f"variables {sorted(msg.variables or {})} do not match {t['variables']}", "message")
        else:
            r.ok("template")
        text = " ".join(str(v) for v in (msg.variables or {}).values())
    else:
        text = " ".join((msg.text or {}).values())
        if len((msg.text or {}).get("title", "")) > copy_rules["title_max_chars"]:
            problems.append("title too long")
        if len((msg.text or {}).get("body", "")) > copy_rules["body_max_chars"]:
            problems.append("body too long")

    forbidden = [p for p in copy_rules.get("forbidden_phrases", FORBIDDEN_DEFAULT) if p.lower() in text.lower()]
    if forbidden:
        problems.append(f"forbidden phrase(s): {forbidden}")
    bad_prices = {int(a or b) for a, b in PRICE_PATTERN.findall(text)} - pack.prices()
    if bad_prices:
        problems.append(f"price claim not in pricing.json: {sorted(bad_prices)}")
    if problems:
        r.fail("copy_rules", "; ".join(problems), "message")
    else:
        r.ok("copy_rules")


def _check_injection(r: CheckResult, proposal: Proposal) -> None:
    """Model output is generated text. If it echoes instruction-like phrases it is not used."""
    texts = [proposal.model.get("reason", "")]
    if proposal.message and proposal.message.text:
        texts += list(proposal.message.text.values())
    hits = [t for t in texts if INJECTION_PATTERN.search(t or "")]
    if hits:
        r.fail("injection", "model text contains instruction-like content; dropping model-written copy", "message")
    else:
        r.ok("injection")
