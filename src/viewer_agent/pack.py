"""Read-only view of the data pack: catalogue, profile snapshot, replay arms and the config files.

The event history is deliberately not loaded here; it lives in the store (see store.py) because the agent reads it
per viewer and the replay harness has to be able to slow that dependency down.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .times import parse_ts

CATALOGUE_DIALECTS = ("d1", "d2", "d3", "d4", "d5", "d6")

# Text that addresses an AI or tries to change its rules. Used to flag catalogue data (which is never followed) and to
# drop model output that echoes such text.
INSTRUCTION_LIKE = re.compile(
    r"(?:ignore|disregard|override) (?:all |any |your |the )?(?:previous|prior|above|earlier|existing)"
    r"|system (?:note|prompt|message)|assistant instruction|ai agents?|llm"
    r"|(?:every|all) (?:users?|viewers?)|opted out|pre-?approved|mark (?:every|all)", re.I)


@dataclass
class Title:
    title_id: str
    slug: str
    name: str
    dialect: str
    language: str
    content_type: str
    genres: list[str]
    release_date: date
    available_from: datetime
    available_until: datetime | None
    is_premium: bool
    # Kept for completeness only. Descriptions are data, never shown to the model (see planner.py).
    description: str = field(repr=False, default="")

    def available_at(self, at: datetime) -> bool:
        if at < self.available_from:
            return False
        return self.available_until is None or at < self.available_until


def _bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() == "true"


class Pack:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.titles: dict[str, Title] = {}
        self.variants: dict[str, list[str]] = defaultdict(list)  # content_slug -> title_ids (language variants)
        self.episodes: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        self.profiles: dict[str, dict] = {}
        self.profile_versions: dict[str, list[dict]] = defaultdict(list)
        self.arms: dict[str, str] = {}
        self.popularity: Counter = Counter()  # play_started per title_id, filled from the history by the store
        self.flagged_text: dict[str, str] = {}  # title_id -> description that reads like an instruction
        self._load()

    # ----------------------------------------------------------------- loading
    def _load(self) -> None:
        with open(self.root / "data" / "catalogue.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = Title(
                    title_id=r["title_id"], slug=r["content_slug"], name=r["title"], dialect=r["dialect"],
                    language=r["language"], content_type=r["content_type"], genres=r["genres"].split("|"),
                    release_date=date.fromisoformat(r["release_date"]),
                    available_from=parse_ts(r["available_from"]), available_until=parse_ts(r["available_until"]),
                    is_premium=_bool(r["is_premium"]) or False, description=r.get("description", ""),
                )
                self.titles[t.title_id] = t
                if INSTRUCTION_LIKE.search(t.description):
                    self.flagged_text[t.title_id] = t.description
                self.variants[t.slug].append(t.title_id)

        with open(self.root / "data" / "episodes.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.episodes[r["content_slug"]].append((int(r["season"]), int(r["episode"]), r["episode_id"]))
        for eps in self.episodes.values():
            eps.sort()

        # The snapshot can hold two rows for one viewer. Keep every version for provenance, use the newest.
        with open(self.root / "data" / "profiles_snapshot.csv", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                for key in ("notifications_opt_in", "whatsapp_opt_in"):
                    r[key] = _bool(r[key])
                self.profile_versions[r["user_id"]].append(r)
        for user_id, rows in self.profile_versions.items():
            rows.sort(key=lambda r: r["updated_at"])
            self.profiles[user_id] = rows[-1]

        with open(self.root / "replay" / "arms.csv", encoding="utf-8") as f:
            self.arms = {r["user_id"]: r["arm"] for r in csv.DictReader(f)}

        cfg = self.root / "config"
        self.goals_config = json.loads((cfg / "goals.json").read_text(encoding="utf-8"))
        self.interventions = json.loads((cfg / "interventions.json").read_text(encoding="utf-8"))
        self.pricing = json.loads((cfg / "pricing.json").read_text(encoding="utf-8"))
        self.policy_text = (cfg / "messaging_policy.md").read_text(encoding="utf-8")

    # ----------------------------------------------------------------- queries
    def title(self, title_id: str | None) -> Title | None:
        return self.titles.get(title_id) if title_id else None

    def quarantined(self, title_id: str | None) -> bool:
        """Titles whose catalogue text tries to instruct an AI are held back from promotion until a person reviews them."""
        return title_id in self.flagged_text

    def available(self, title_id: str | None, at: datetime) -> bool:
        t = self.title(title_id)
        return t is not None and t.available_at(at)

    def withdraw(self, title_id: str, at: datetime) -> None:
        """A control event ended a licence early. From `at` on the title must not be shown or promoted."""
        t = self.titles.get(title_id)
        if t and (t.available_until is None or at < t.available_until):
            t.available_until = at

    def dialect_titles(self, dialect: str, at: datetime, language: str | None = None) -> list[Title]:
        """Titles the app will accept on a page for `dialect`: right dialect, available now, one per content_slug.

        When a title exists in two languages we keep the one matching the app language, else the first one.
        """
        by_slug: dict[str, Title] = {}
        for t in self.titles.values():
            if t.dialect != dialect or not t.available_at(at) or self.quarantined(t.title_id):
                continue
            current = by_slug.get(t.slug)
            if current is None or (language and t.language == language and current.language != language):
                by_slug[t.slug] = t
        return list(by_slug.values())

    def prices(self) -> set[int]:
        return {p["price_inr"] for p in self.pricing["plans"]}

    def plan_price(self, plan_id: str) -> int | None:
        for p in self.pricing["plans"]:
            if p["plan_id"] == plan_id:
                return p["price_inr"]
        return None
