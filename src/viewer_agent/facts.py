"""Assemble what is known about one viewer at one moment, with the source and time of every fact.

Precedence is decided per fact. No source wins just because it is newer or bigger:
  * dialect              app setting on the trigger > profile snapshot > campaign deeplink
                         (the app only shows a page for the dialect it is set to, so that is the one that matters)
  * subscription status  a purchase / renewal / lapse event received after the snapshot was updated beats the snapshot
  * consent              snapshot flag, overridden by a later notification_opt_out event
  * signup, plays, completions, visits   always from events
Conflicts are kept, never silently resolved.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .pack import CATALOGUE_DIALECTS, Pack
from .times import iso, ist_day, parse_ts

STATUS_EVENTS = {"purchase_completed", "subscription_renewed", "subscription_lapsed"}
DEEPLINK = re.compile(r"^ott://c/(?P<campaign>[^/]+)/(?P<dialect>[^/]+)/title/(?P<title_id>[^/]+)$")


@dataclass
class Fact:
    name: str
    value: Any
    source: str  # app_setting | profile | event | deeplink | derived | none
    observed_at: str | None
    evidence: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {"value": self.value, "source": self.source, "observed_at": self.observed_at,
                "evidence": self.evidence, "note": self.note}


@dataclass
class ViewerFacts:
    user_id: str
    now: datetime
    trigger: dict
    facts: dict[str, Fact] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    has_profile: bool = False

    def add(self, fact: Fact) -> None:
        self.facts[fact.name] = fact

    def value(self, name: str, default: Any = None) -> Any:
        fact = self.facts.get(name)
        return default if fact is None else fact.value

    def known(self) -> bool:
        """False when we hold nothing about this viewer beyond the open itself: no profile row, no other events."""
        return self.has_profile or any(e["event_id"] != self.trigger.get("event_id") for e in self.events)

    def snapshot(self) -> dict:
        return {name: f.as_dict() for name, f in self.facts.items()}

    def evidence(self) -> list[dict]:
        """Every evidence ref once, with its own time and its age at decision time (its freshness)."""
        times = {e["event_id"]: e["_at"] for e in self.events}
        out, seen = [], set()
        for f in self.facts.values():
            for ref in f.evidence:
                if ref in seen:
                    continue
                seen.add(ref)
                kind, _, key = ref.partition(":")
                at = None
                if kind in ("event", "trigger"):
                    at = times.get(key) or (parse_ts(self.trigger["received_at"]) if kind == "trigger" else None)
                elif kind == "profile":
                    at = parse_ts(key.split("@", 1)[1])
                at = at or parse_ts(f.observed_at)
                out.append({"ref": ref, "source": f.source, "observed_at": iso(at) if at else None,
                            "age_s": int((self.now - at).total_seconds()) if at else None})
        return out

    def confidence(self) -> tuple[float, list[str]]:
        """How complete and consistent the evidence is. This is about the data, not about any model's opinion."""
        score, why = 0.4, []
        if self.has_profile:
            score += 0.15
            why.append("profile row present")
        else:
            why.append("no profile row")
        depth = self.value("history_depth", 0)
        if depth >= 10:
            score += 0.1
            why.append(f"{depth} events of history")
        elif depth == 0:
            score -= 0.15
            why.append("no event history")
        else:
            why.append(f"only {depth} events of history")
        if (self.value("dialect") or {}).get("conflicts"):
            score -= 0.15
            why.append("dialect sources disagree")
        else:
            score += 0.05
            why.append("dialect sources agree")
        if (self.value("trigger_freshness") or {}).get("event_age_s", 0) <= 60:
            score += 0.05
            why.append("trigger is fresh")
        return round(score, 2), why  # rounded so the number shown and the thresholds always agree


def event_time(e: dict) -> datetime:
    """When the event really happened. The device clock can be hours off, but the gap between a phone's own
    occurred_at and sent_at is reliable, so we subtract that buffering delay from the trusted server time."""
    received = parse_ts(e["received_at"])
    sent, occurred = parse_ts(e.get("sent_at")), parse_ts(e.get("occurred_at"))
    if sent and occurred and sent >= occurred:
        return received - (sent - occurred)
    return received


def parse_deeplink(link: str | None) -> dict | None:
    m = DEEPLINK.match(link or "")
    return m.groupdict() if m else None


def build_facts(user_id: str, trigger: dict, events: list[dict], pack: Pack, now: datetime,
                settings: Settings) -> ViewerFacts:
    fx = ViewerFacts(user_id=user_id, now=now, trigger=trigger)
    profile = pack.profiles.get(user_id)
    fx.has_profile = profile is not None
    profile = profile or {}
    profile_ref = f"profile:{user_id}@{profile['updated_at']}" if profile else None
    profile_updated = parse_ts(profile.get("updated_at"))

    # Keep every event once, with its real time, and drop this trigger's own session from "history".
    seen: set[str] = set()
    for e in sorted(events, key=lambda e: e["received_at"]):
        if e["event_id"] in seen:
            continue
        seen.add(e["event_id"])
        e = dict(e)
        e["_at"] = event_time(e)
        fx.events.append(e)
    history = [e for e in fx.events if e["session_id"] != trigger.get("session_id")]

    versions = pack.profile_versions.get(user_id, [])
    if len(versions) > 1:
        fx.conflicts.append("profile snapshot has %d rows for this viewer (%s); used the newest updated_at" % (
            len(versions), ", ".join(f"{v['subscription_status']}/{v['preferred_dialect']}@{v['updated_at'][:10]}"
                                     for v in versions)))

    _trigger_freshness(fx, settings)
    _dialect(fx, profile, profile_ref)
    _signup(fx, profile, profile_ref)
    _subscription(fx, profile, profile_ref, profile_updated)
    _trial_end(fx, profile, profile_ref, pack)
    _consent(fx, profile, profile_ref, profile_updated)
    _viewing(fx, history, pack)
    _last_visit(fx, history)
    fx.add(Fact("history_depth", len(fx.events), "derived", iso(now),
                [f"events:{user_id}:count={len(fx.events)}"]))
    return fx


# --------------------------------------------------------------------------- individual facts
def _trigger_freshness(fx: ViewerFacts, settings: Settings) -> None:
    t = fx.trigger
    received, sent, occurred = parse_ts(t["received_at"]), parse_ts(t.get("sent_at")), parse_ts(t.get("occurred_at"))
    # A phone whose clock is wrong is still right about how long it held the event: sent minus occurred is the
    # buffering delay even when both are hours off. Only when occurred is after sent is that gap unusable.
    clock_ok = bool(sent and occurred and occurred <= received + timedelta(seconds=60))
    gap_ok = bool(sent and occurred and occurred <= sent)
    age_s = max(0, int((fx.now - event_time(t)).total_seconds()))
    live = age_s <= settings.live_visit_max_age_s
    note = ""
    if not gap_ok:
        note = "occurred_at is after sent_at; buffering delay unknown, used received_at"
        fx.assumptions.append("trigger timestamps are inconsistent (occurred_at after sent_at); treated as a live open")
    elif not clock_ok and live:
        note = "device clock is ahead of the server; the buffering delay it reports is still used"
    elif not live:
        note = f"buffered on the device for {age_s // 3600}h{(age_s % 3600) // 60:02d}m; not a live visit"
    fx.add(Fact("trigger_freshness", {"event_age_s": age_s, "live_visit": live, "device_clock_trusted": clock_ok},
                "event", iso(received), [f"trigger:{t['event_id']}"], note))


def _dialect(fx: ViewerFacts, profile: dict, profile_ref: str | None) -> None:
    t = fx.trigger
    app = t.get("app_dialect")
    app = app if app and app != "NA" else None
    prof = profile.get("preferred_dialect") or None
    link = parse_deeplink((t.get("properties") or {}).get("deeplink"))
    link_dialect = link["dialect"] if link else None

    if app:
        used, source, ref = app, "app_setting", f"trigger:{t['event_id']}"
    elif prof:
        used, source, ref = prof, "profile", profile_ref
        fx.assumptions.append("app dialect missing on the trigger; profile preference used for messages only")
    else:
        used, source, ref = None, "none", None

    conflicts = []
    for name, value in (("profile", prof), ("deeplink", link_dialect), ("app_setting", app)):
        if value and used and value != used:
            conflicts.append(f"{name}:{value}")
    if conflicts:
        fx.conflicts.append(f"dialect: app_setting={app}, profile={prof}, deeplink={link_dialect} -> used {used} ({source})")

    note = ""
    if app and app not in CATALOGUE_DIALECTS:
        note = f"app dialect {app} has no catalogue content"
    page_dialect = app if app in CATALOGUE_DIALECTS else None
    message_dialect = page_dialect or (prof if prof in CATALOGUE_DIALECTS else None)

    fx.add(Fact("dialect", {"used": used, "source": source, "conflicts": conflicts, "page_dialect": page_dialect,
                            "message_dialect": message_dialect}, source, iso(parse_ts(t["received_at"])),
                [r for r in (ref,) if r], note))
    fx.add(Fact("app_language", t.get("app_language"), "app_setting", iso(parse_ts(t["received_at"])),
                [f"trigger:{t['event_id']}"]))
    if link:
        fx.add(Fact("campaign_deeplink", link, "deeplink", iso(parse_ts(t["received_at"])),
                    [f"trigger:{t['event_id']}"], "campaign intent, not the dialect the app shows"))


def _signup(fx: ViewerFacts, profile: dict, profile_ref: str | None) -> None:
    signup_events = [e for e in fx.events if e["event"] == "signup_completed"]
    prof_signup = parse_ts(profile.get("signup_at"))
    if signup_events:
        e = signup_events[0]
        if prof_signup and abs((e["_at"] - prof_signup).total_seconds()) > 86400:
            fx.conflicts.append(f"signup_at: profile={iso(prof_signup)}, event={iso(e['_at'])} -> used event")
        fx.add(Fact("signup_at", iso(e["_at"]), "event", iso(e["_at"]), [f"event:{e['event_id']}"]))
    elif prof_signup:
        fx.add(Fact("signup_at", iso(prof_signup), "profile", profile.get("updated_at"), [profile_ref]))
    else:
        fx.add(Fact("signup_at", None, "none", None, [], "no profile row and no signup event"))


def _subscription(fx: ViewerFacts, profile: dict, profile_ref: str | None, profile_updated: datetime | None) -> None:
    status = profile.get("subscription_status") or None
    source, observed, evidence = ("profile", profile.get("updated_at"), [profile_ref]) if status else ("none", None, [])
    paid_refs, paid_before_snapshot = [], False

    for e in fx.events:
        props = e["properties"]
        paid = (e["event"] == "purchase_completed" and not props.get("is_trial")) or e["event"] == "subscription_renewed"
        if paid:
            paid_refs.append(f"event:{e['event_id']}")
            paid_before_snapshot |= bool(profile_updated and e["_at"] <= profile_updated)
        if e["event"] not in STATUS_EVENTS:
            continue
        if e["event"] == "purchase_completed":
            new = "trial_active" if props.get("is_trial") else "active"
        elif e["event"] == "subscription_renewed":
            new = "active"
        else:
            new = "lapsed"
        # only events the snapshot cannot have seen yet are allowed to override it
        if profile_updated is None or e["_at"] > profile_updated:
            if status and new != status:
                fx.conflicts.append(f"subscription_status: profile={status} ({profile.get('updated_at')}), "
                                    f"{e['event']} at {iso(e['_at'])} -> used {new}")
            status, source, observed, evidence = new, "event", iso(e["_at"]), [f"event:{e['event_id']}"]

    if status is None:
        if paid_refs:
            status, source, observed, evidence = "active", "derived", iso(fx.now), paid_refs[-1:]
        elif fx.value("signup_at") and fx.facts["signup_at"].source == "event":
            status, source, observed, evidence = "never", "derived", iso(fx.now), fx.facts["signup_at"].evidence
            fx.assumptions.append("no profile row; status 'never' inferred from signup_completed with no purchase")
        else:
            status = "unknown"
    fx.add(Fact("subscription_status", status, source, observed, evidence))

    ever_paid = bool(paid_refs) or profile.get("subscription_status") in ("active", "lapsed") or bool(profile.get("plan_started_at"))
    if paid_before_snapshot and profile.get("subscription_status") == "never":
        fx.conflicts.append("profile says 'never' but events before the snapshot show a paid purchase; treated as having paid")
    fx.add(Fact("ever_paid", ever_paid, "event" if paid_refs else ("profile" if profile else "none"),
                observed if paid_refs else profile.get("updated_at"), paid_refs or ([profile_ref] if profile_ref else [])))

    lapses = [e for e in fx.events if e["event"] == "subscription_lapsed"]
    if lapses:
        fx.add(Fact("lapsed_at", iso(lapses[-1]["_at"]), "event", iso(lapses[-1]["_at"]), [f"event:{lapses[-1]['event_id']}"]))
    elif status == "lapsed" and profile.get("plan_ends_at"):
        fx.add(Fact("lapsed_at", profile["plan_ends_at"], "profile", profile.get("updated_at"), [profile_ref],
                    "plan_ends_at used as the lapse time"))
    else:
        fx.add(Fact("lapsed_at", None, "none", None, []))


def trial_end_from(purchase: dict, pack: Pack) -> datetime | None:
    """A trial purchase ends after the plan's days in pricing.json."""
    for plan in pack.pricing["plans"]:
        if plan["plan_id"] == purchase["properties"].get("plan_id"):
            return event_time(purchase) + timedelta(days=plan["days"])
    return None


def _trial_end(fx: ViewerFacts, profile: dict, profile_ref: str | None, pack: Pack) -> None:
    status = fx.facts["subscription_status"]
    if status.value != "trial_active":
        fx.add(Fact("trial_ends_at", None, "none", None, []))
        return
    if status.source == "event":  # a trial bought after the snapshot: its end follows from the plan length
        purchase = next(e for e in fx.events if f"event:{e['event_id']}" in status.evidence)
        end = trial_end_from(purchase, pack)
        fx.add(Fact("trial_ends_at", iso(end) if end else None, "derived", status.observed_at, status.evidence,
                    "purchase time plus the plan's days in pricing.json"))
    else:
        fx.add(Fact("trial_ends_at", profile.get("plan_ends_at") or None, "profile", profile.get("updated_at"),
                    [profile_ref] if profile_ref else []))


def _consent(fx: ViewerFacts, profile: dict, profile_ref: str | None, profile_updated: datetime | None) -> None:
    for channel, flag in (("push", "notifications_opt_in"), ("whatsapp", "whatsapp_opt_in")):
        value = profile.get(flag) if profile else None
        source, observed, evidence = ("profile", profile.get("updated_at"), [profile_ref]) if profile else ("none", None, [])
        for e in fx.events:
            if e["event"] == "notification_opt_out" and e["properties"].get("channel") == channel:
                if profile_updated is None or e["_at"] > profile_updated:
                    if value:
                        fx.conflicts.append(f"consent_{channel}: profile=true, opt-out event at {iso(e['_at'])} -> used false")
                    value, source, observed, evidence = False, "event", iso(e["_at"]), [f"event:{e['event_id']}"]
        fx.add(Fact(f"consent_{channel}", value, source, observed, evidence,
                    "" if profile else "no profile row, consent unknown"))


def _viewing(fx: ViewerFacts, history: list[dict], pack: Pack) -> None:
    plays = [e for e in fx.events if e["event"] == "play_started"]
    completions = [e for e in fx.events if e["event"] == "episode_completed"]
    genres: Counter = Counter()
    played_slugs: dict[str, dict] = {}
    for e in plays:
        t = pack.title(e["properties"].get("title_id"))
        if t:
            genres.update(t.genres)
            if t.content_type != "film":
                if t.slug not in played_slugs or e["_at"] > played_slugs[t.slug]["_at"]:
                    played_slugs[t.slug] = {"_at": e["_at"], "title_id": t.title_id, "event_id": e["event_id"]}
    completed: dict[str, set] = {}
    for e in completions:
        t = pack.title(e["properties"].get("title_id"))
        if t:
            completed.setdefault(t.slug, set()).add(e["properties"].get("episode_id"))

    cutoff = fx.now - timedelta(days=14)
    play_days = sorted({str(ist_day(e["_at"])) for e in plays if cutoff <= e["_at"] <= fx.now})
    refs = [f"event:{e['event_id']}" for e in plays[-3:]]
    fx.add(Fact("plays", {"count": len(plays), "first_at": iso(plays[0]["_at"]) if plays else None,
                          "last_at": iso(plays[-1]["_at"]) if plays else None, "days_in_last_14": play_days,
                          "top_genres": [g for g, _ in genres.most_common(3)]},
                "event", iso(plays[-1]["_at"]) if plays else None,
                refs or [f"events:{fx.user_id}:play_started=0"]))
    fx.add(Fact("completions", {"count": len(completions), "last_at": iso(completions[-1]["_at"]) if completions else None},
                "event", iso(completions[-1]["_at"]) if completions else None,
                [f"event:{e['event_id']}" for e in completions[-2:]] or [f"events:{fx.user_id}:episode_completed=0"]))

    unfinished = []
    for slug, last in played_slugs.items():
        episodes = pack.episodes.get(slug) or []
        watched = completed.get(slug, set())
        remaining = [ep for ep in episodes if ep[2] not in watched]
        if episodes and remaining:
            season, number, episode_id = remaining[0]
            unfinished.append({"slug": slug, "title_id": last["title_id"], "last_played_at": iso(last["_at"]),
                               "next_episode_id": episode_id, "next_episode_number": number,
                               "watched": len(watched), "total": len(episodes), "evidence": f"event:{last['event_id']}"})
    unfinished.sort(key=lambda u: u["last_played_at"], reverse=True)
    fx.add(Fact("unfinished_series", unfinished, "event",
                unfinished[0]["last_played_at"] if unfinished else None, [u["evidence"] for u in unfinished[:3]]))


def _last_visit(fx: ViewerFacts, history: list[dict]) -> None:
    trigger_time = event_time(fx.trigger)
    opens = [e for e in history if e["event"] in ("app_open", "login_success") and e["_at"] < trigger_time]
    if opens:
        last = max(opens, key=lambda e: e["_at"])
        fx.add(Fact("last_visit_before_trigger", iso(last["_at"]), "event", iso(last["_at"]), [f"event:{last['event_id']}"]))
    else:
        fx.add(Fact("last_visit_before_trigger", None, "none", None, [], "no earlier visit on record"))
