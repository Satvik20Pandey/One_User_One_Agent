"""Tunable settings.

Values marked "brief" come straight from the assignment. The rest are our own assumptions where the brief or the
pack left a rule open; each one is listed in KNOWN_LIMITATIONS.md.
"""
from __future__ import annotations

from dataclasses import dataclass


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

    # assumption: order used when a viewer is eligible for several goals (shortest window / first value first)
    goal_priority: tuple[str, ...] = ("G01", "G03", "G02", "G07", "G04", "G12", "G06", "G09", "G08", "G05")

    # design choice: messages go out the next evening, never into the session the viewer is already in
    message_send_hour_ist: int = 19

    # design choice: how long we wait for the model before falling back to the rule-based decision
    model_timeout_s: float = 10.0

    # from control_events.jsonl "dependency_degraded": p95 read latency 8 s and 20% of reads time out.
    # A timed-out read costs the full 8 s we waited for it; a read that succeeds is charged as merely slow.
    store_degraded_timeout_s: float = 8.0
    store_degraded_timeout_rate: float = 0.2
    store_degraded_slow_s: float = 2.0
