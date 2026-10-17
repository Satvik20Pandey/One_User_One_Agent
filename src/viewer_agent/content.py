"""Builds the content of an action from the catalogue, in the dialect the viewer's app will actually show.

Nothing here reads descriptions or free text. Titles are picked by dialect, availability, popularity and genre.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .config import Settings
from .facts import ViewerFacts
from .pack import Pack, Title
from .times import next_send_slot

MIN_ROW, MAX_ROW, MAX_ROWS = 3, 10, 6
MONEY_GOALS = {"G04", "G05", "G06"}
PUSH_TITLE_MAX, PUSH_BODY_MAX = 40, 120


@dataclass
class Row:
    row_type: str
    title_ids: list[str]


@dataclass
class PageDraft:
    dialect: str
    hero_title_id: str | None
    rows: list[Row]
    candidate_ids: list[str] = field(default_factory=list)
    hero_reason: str = "most popular"  # campaign link | goal series | most popular

    def title_ids(self) -> list[str]:
        ids = [t for r in self.rows for t in r.title_ids]
        return ([self.hero_title_id] if self.hero_title_id else []) + ids


@dataclass
class MessageDraft:
    channel: str  # whatsapp | push | in_app_message
    dialect: str
    title_id: str
    send_at: datetime
    template_id: str | None = None
    variables: dict | None = None
    text: dict | None = None  # {"title", "body"} for push and in-app
    deeplink: str | None = None
    hook: str = ""


def _rank(titles: list[Title], pack: Pack, premium_first: bool = False) -> list[Title]:
    """Most played first, newer first on ties. Money goals put premium titles ahead."""
    def key(t: Title):
        return (0 if premium_first and t.is_premium else 1, -pack.popularity.get(t.title_id, 0), -t.release_date.toordinal())
    return sorted(titles, key=key)


def _series_first(facts: ViewerFacts, pack: Pack, dialect: str, at: datetime, context: dict | None) -> list[dict]:
    """Unfinished series the app can show, with the one the goal is about (if any) first."""
    goal_slug = (context or {}).get("slug")
    series = [s for s in facts.value("unfinished_series") or []
              if pack.title(s["title_id"]) and pack.title(s["title_id"]).dialect == dialect
              and pack.title(s["title_id"]).available_at(at)]
    return sorted(series, key=lambda s: s["slug"] != goal_slug)


def build_page(goal_id: str, facts: ViewerFacts, pack: Pack, now: datetime, settings: Settings,
               context: dict | None = None) -> PageDraft | None:
    dialect = (facts.value("dialect") or {}).get("page_dialect")
    if not dialect:
        return None
    pool = pack.dialect_titles(dialect, now, facts.value("app_language"))
    if len(pool) < MIN_ROW:
        return None
    premium_first = goal_id in MONEY_GOALS
    ranked = _rank(pool, pack, premium_first)
    played_slugs = {s["slug"] for s in facts.value("unfinished_series") or []}
    top_genres = (facts.value("plays") or {}).get("top_genres") or []
    recent_cutoff = (now - timedelta(days=30)).date()

    # hero: the campaign title if it is in this dialect, else the strongest title for the goal
    link = facts.value("campaign_deeplink") or {}
    hero, hero_reason = None, "most popular"
    campaign_title = pack.title(link.get("title_id"))
    if campaign_title and campaign_title.dialect == dialect and campaign_title.available_at(now):
        hero, hero_reason = campaign_title, "campaign link"
    unfinished = _series_first(facts, pack, dialect, now, context)
    if hero is None and goal_id == "G07" and unfinished:
        hero, hero_reason = pack.title(unfinished[0]["title_id"]), "goal series"
    if hero is None:
        hero = ranked[0]

    used = {hero.slug}

    def take(candidates: list[Title]) -> list[str]:
        ids = []
        for t in candidates:
            if t.slug in used or not t.available_at(now):
                continue
            used.add(t.slug)
            ids.append(t.title_id)
            if len(ids) == MAX_ROW:
                break
        return ids

    def popular():
        return Row("popular_in_dialect", take(ranked))

    def new_releases():
        return Row("new_releases", take([t for t in ranked if t.release_date >= recent_cutoff]))

    def because_you_watched():
        if not top_genres:
            return Row("because_you_watched", [])
        return Row("because_you_watched", take([t for t in ranked if top_genres[0] in t.genres]))

    def genre_row():
        counts = Counter(g for t in ranked for g in t.genres)
        for g, _ in counts.most_common():
            if g not in top_genres[:1]:
                ids = take([t for t in ranked if g in t.genres])
                if ids:
                    return Row("genre", ids)
        return Row("genre", [])

    def next_episode():
        first = [pack.title(s["title_id"]) for s in unfinished]
        shows = [t for t in ranked if t.content_type != "film" and t.slug not in played_slugs]
        return Row("next_episode", take(first + shows))

    if goal_id == "G07":
        builders = [next_episode, because_you_watched, popular, new_releases]
    elif goal_id == "G08":
        builders = [new_releases, popular, because_you_watched, genre_row]
    elif goal_id in ("G09", "G12"):
        builders = [because_you_watched, popular, new_releases, genre_row]
    else:  # G01, G02, G03 and the money goals
        builders = [popular, new_releases, because_you_watched, genre_row]

    rows = [r for r in (b() for b in builders) if len(r.title_ids) >= MIN_ROW][:MAX_ROWS]
    if not rows:
        return None
    return PageDraft(dialect, hero.title_id, rows, candidate_ids=[t.title_id for t in ranked], hero_reason=hero_reason)


def build_message(goal_id: str, facts: ViewerFacts, pack: Pack, now: datetime, settings: Settings,
                  context: dict | None = None) -> MessageDraft | None:
    """One message, only when the goal has a concrete content hook. Channel: WhatsApp if the viewer opted in, else
    push if opted in, else an in-app message (no consent needed; the app shows it at the next open)."""
    if facts.value("consent_whatsapp") is True:
        channel = "whatsapp"
    elif facts.value("consent_push") is True:
        channel = "push"
    else:
        channel = "in_app_message"
    dialect_fact = facts.value("dialect") or {}
    # an in-app message is read inside the app, so it must be in the app's dialect
    dialect = dialect_fact.get("page_dialect" if channel == "in_app_message" else "message_dialect")
    if not dialect:
        return None
    send_at = now if channel == "in_app_message" else next_send_slot(now, settings.message_send_hour_ist)
    hook = _hook(goal_id, facts, pack, dialect, send_at, now, context)
    if hook is None:
        return None
    title, name, (push_title, push_body), template = hook
    msg = MessageDraft(channel, dialect, title.title_id, send_at, hook=name)
    if channel == "whatsapp":
        msg.template_id, msg.variables = template
    else:
        msg.text = _push_copy(push_title, push_body)
        msg.deeplink = f"ott://title/{title.title_id}"
    return msg


def _hook(goal_id: str, facts: ViewerFacts, pack: Pack, dialect: str, at: datetime, now: datetime, context: dict | None):
    """(title, hook name, push copy, WhatsApp template) for goals that have something concrete to say, else None."""
    pool = pack.dialect_titles(dialect, at, facts.value("app_language"))
    if not pool:
        return None
    recommend = lambda t: ("wa_recommend_title_v1", {"title_name": t.name, "dialect_name": dialect.upper()})  # noqa: E731

    if goal_id == "G07":
        # only the series the goal will be judged on; a message about another one would not help this goal
        series = _series_first(facts, pack, dialect, at, context)
        if not series or series[0]["slug"] != (context or {}).get("slug"):
            return None
        s = series[0]
        t = pack.title(s["title_id"])
        # not wa_new_episode_v2: "Naya episode aa gaya" would claim a new episode, and the next one is not new
        return t, "unfinished series", (f"Continue {t.name}", f"Episode {s['next_episode_number']} of {t.name} is waiting for you."), recommend(t)

    if goal_id == "G08":
        recent = [t for t in _rank(pool, pack) if t.release_date >= (now - timedelta(days=30)).date()]
        if not recent:
            return None
        t = recent[0]
        return t, "new release", (f"New in {dialect.upper()}: {t.name}", f"{t.name} just released. Start watching now."), recommend(t)

    if goal_id == "G04":
        price = pack.plan_price("P_MONTH")
        t = _rank(pool, pack, premium_first=True)[0]
        return (t, "trial ending", ("Keep watching after your trial", f"Continue with the monthly plan at Rs {price}."),
                ("wa_trial_ending_v1", {"plan_price": f"Rs {price}"}))
    return None


def _push_copy(title: str, body: str) -> dict:
    return {"title": title[:PUSH_TITLE_MAX], "body": body[:PUSH_BODY_MAX]}
