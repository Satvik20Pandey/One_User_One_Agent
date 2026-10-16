"""Shared helpers for the tests: a tiny pack, trigger and event factories, and a ready-to-use agent environment."""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from viewer_agent.agent import Deps, ViewerAgent
from viewer_agent.config import Settings
from viewer_agent.facts import build_facts
from viewer_agent.goals import RuleBook
from viewer_agent.pack import Pack
from viewer_agent.planner import MockProvider
from viewer_agent.store import Store
from viewer_agent.times import iso, parse_ts

MINI = Path(__file__).parent / "fixtures" / "mini_pack"
NOW = parse_ts("2026-10-01T05:00:00.000Z")  # 10:30 IST, outside quiet hours
_ids = itertools.count(1)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def make_env(tmp_path: Path, settings: Settings | None = None) -> SimpleNamespace:
    settings = settings or Settings()
    pack = Pack(MINI)
    store = Store(tmp_path / "state.db")
    stats = store.ingest_history(MINI / "data" / "events.jsonl")
    pack.popularity = stats["plays_per_title"]
    rules = RuleBook(pack.goals_config, settings, known_events=stats["event_names"])
    provider = MockProvider(read_jsonl(MINI / "replay" / "model_faults.jsonl"))
    deps = Deps(pack, store, rules, settings, provider)

    def decide(trigger: dict, at=None):
        store.add_event(trigger)
        return ViewerAgent(trigger["user_id"], deps).handle_trigger(trigger, at or parse_ts(trigger["deliver_at"]))

    def facts_for(user_id: str, trigger: dict | None = None, at=None):
        trigger = trigger or make_trigger(user_id)
        return build_facts(user_id, trigger, store.events_for(user_id), pack, at or NOW, settings)

    return SimpleNamespace(pack=pack, store=store, rules=rules, settings=settings, provider=provider, deps=deps,
                           stats=stats, decide=decide, facts_for=facts_for)


def make_trigger(user_id: str, at=NOW, dialect: str | None = "d1", event_id: str | None = None,
                 session: str | None = None, occurred_at: str | None = None, sent_at: str | None = None,
                 deeplink: str | None = None, language: str = "hin", event: str = "app_open") -> dict:
    n = next(_ids)
    when = iso(at)
    return {"event_id": event_id or f"trg-{user_id}-{n}", "user_id": user_id, "event": event,
            "occurred_at": occurred_at or when, "sent_at": sent_at or when, "received_at": when,
            "session_id": session or f"sess-{user_id}-{n}", "app_dialect": dialect, "app_language": language,
            "platform": "android", "app_version": "6.1.4",
            "properties": {"deeplink": deeplink} if deeplink else {}, "deliver_at": when}


def make_event(user_id: str, event: str, at, props: dict | None = None, session: str | None = None,
               event_id: str | None = None, dialect: str = "d1") -> dict:
    n = next(_ids)
    when = iso(at)
    return {"event_id": event_id or f"evt-{user_id}-{n}", "user_id": user_id, "event": event, "occurred_at": when,
            "sent_at": when, "received_at": when, "session_id": session or f"sess-{user_id}-{n}",
            "app_dialect": dialect, "app_language": "hin", "platform": "android", "app_version": "6.1.4",
            "properties": props or {}, "deliver_at": when}


def failed_checks(result) -> list[str]:
    return [f["check"] for f in result.failed]
