"""Tunable settings.

Values marked "brief" come straight from the assignment. The rest are our own assumptions where the brief or the
pack left a rule open; each one is listed in KNOWN_LIMITATIONS.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Which goal matters most depends on where the viewer is in their lifecycle. Within a stage, goals with a broad,
# observable success signal come before narrow ones, so the holdout comparison has enough signal to say something.
# (In the sample run, "resume this exact series" succeeded about 4% of the time; "play any new release" about 29%.)
GOAL_PRIORITY_BY_STAGE: dict[str, tuple[str, ...]] = {
    "trial_active": ("G01", "G03", "G04", "G02", "G07", "G12"),
    "active": ("G01", "G03", "G09", "G08", "G02", "G07", "G12"),
    "lapsed": ("G06", "G12", "G02", "G07"),
    "never": ("G01", "G03", "G02", "G12", "G07", "G05"),
    "unknown": ("G01", "G03", "G02", "G12", "G07"),
}


@dataclass(frozen=True)
class Settings:
    # brief: latency and call budgets per decision
    latency_budget_s: int = 60
    latency_fail_s: int = 300
    max_model_calls: int = 8
    max_tool_calls: int = 20

    # brief / interventions.json: messaging policy
    quiet_start_hour_ist: int = 22
    quiet_end_hour_ist: int = 8
    home_page_ttl_min: int = 120

    # assumption: a trigger whose device-side delay is larger than this was buffered offline and is not a live visit.
    # The pack simulator uses the same 600 s cut-off when it decides which triggers are real sessions.
    live_visit_max_age_s: int = 600

    # assumption: goals.json says "recently lapsed" for G06 without a number
    g06_recent_days: int = 30

    # assumption: goal order per lifecycle stage (see GOAL_PRIORITY_BY_STAGE above)
    goal_priority: dict = field(default_factory=lambda: dict(GOAL_PRIORITY_BY_STAGE))

    # design choice: a push or WhatsApp interrupts the viewer outside the app, so it needs stronger evidence than a
    # home page; below this confidence the agent publishes the page only
    message_min_confidence: float = 0.6

    # design choice: a goal that failed for this viewer is not retried for this many days
    retry_failed_goal_days: int = 7

    # design choice: messages go out the next evening, never into the session the viewer is already in
    message_send_hour_ist: int = 19

    # design choice: how long we wait for the model before falling back to the rule-based decision
    model_timeout_s: float = 10.0
    # mock mode: what a model call costs in time, so latency figures are realistic
    mock_model_latency_s: float = 1.2

    # assumption: decision workers running in parallel; each takes the next app open from one shared queue
    workers: int = 4
    # a 10-minute window with this many times the opens of the hour before is a traffic surge; during a surge the
    # agent skips optional model calls so decisions stay fast
    surge_ratio: float = 2.5
    surge_min_opens: int = 30

    # from control_events.jsonl "dependency_degraded": p95 read latency 8 s and 20% of reads time out.
    # A timed-out read costs the full 8 s we waited for it; a read that succeeds is charged as merely slow.
    store_degraded_timeout_s: float = 8.0
    store_degraded_timeout_rate: float = 0.2
    store_degraded_slow_s: float = 2.0
