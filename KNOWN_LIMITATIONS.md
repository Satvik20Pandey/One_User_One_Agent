# Known limitations

What the brief or the pack left open, how I settled it, and what I would do next. Every assumption is a setting in
`config.py` or a named constant in `goals.py`, so it can be changed in one place.

## Assumptions

| Rule in the code | Status | Basis |
|---|---|---|
| A trigger buffered on the device for more than 10 minutes is not a live visit (`live_visit_max_age_s`) | assumption | the pack simulator uses the same 600 s cut-off; a page lives 120 minutes, so acting on a days-old open helps nobody |
| "Recently lapsed" for G06 means within 30 days (`g06_recent_days`) | assumption | `goals.json` gives no number |
| G10 and G11 are never chosen | derived from data | G10 names no success event; `tv_app_open` never occurs in 142,906 events (checked at start-up, not hard-coded) |
| Goal order per lifecycle stage (`GOAL_PRIORITY_BY_STAGE`) | assumption | new viewers: first value; trial: convert; subscribers: habit and new releases; lapsed: come back; free viewers: first full episode, then resume. Broad success signals before narrow ones, so the holdout comparison has power |
| Page dialect is the app's dialect on the trigger; profile and deeplink are fallbacks for push and WhatsApp only | derived | `interventions.json`: the app requests the page of the dialect it is set to |
| Below evidence confidence 0.4 the agent does not act (`min_confidence_to_act`) | design choice | "not enough evidence" is a valid decision in the brief; the goal is still recorded so the holdout comparison stays fair |
| A push or WhatsApp needs evidence confidence of at least 0.6 (`message_min_confidence`); otherwise page only | design choice | a message outside the app is the action most likely to land in the wrong dialect or annoy; a page is always in the app's dialect |
| Titles whose catalogue text tries to instruct an AI are not promoted until reviewed | design choice | the text is ignored either way; holding the title back is the cautious response to tampered data |
| "Trial ending" copy only when the trial ends within 3 days of the send time | design choice | the template says "your trial is ending soon"; it must be true when sent |
| Messages only for goals with a concrete hook (resume, new release, trial ending), sent at the next 19:00 IST at least an hour away; in-app at the next open | design choice | proportionate actions inside quiet hours and caps |
| A goal that failed for a viewer is not retried for 7 days (`retry_failed_goal_days`) | design choice | learn from the outcome instead of repeating the same plan |
| G06 success counts any `purchase_completed`, including the 1 rupee trial | assumption | `goals.json` says "any paid plan" |
| Four decision workers share one queue (`workers`); a surge is 2.5 times the previous hour's rate (`surge_ratio`) | assumption | sized for the replay; the latency report shows the queue never formed with four |
| The mock model costs 1.2 s per call (`mock_model_latency_s`) | assumption | keeps latency figures realistic in mock mode |
| Confidence = 0.4 + 0.15 profile + 0.1 history depth + 0.05 dialect agreement + 0.05 fresh trigger, minus 0.15 for a dialect conflict, 0.2 for a fallback, 0.1 for a veto | heuristic | the brief asks for a number with a reason; it measures evidence completeness, not a model's self-report |

## Left open

- **No real model run.** The OpenAI-compatible adapter is tested with a fake client only. The sample run uses the
  mock, which reproduces every scripted fault in `model_faults.jsonl`, so evaluators need no key.
- **One effect estimate is noisy.** On the sample run the agent-minus-holdout interval includes zero. Across other
  simulator seeds the estimate moves by a few points. The report says "not proven" instead of claiming a win.
- **Attribution is about order, not cause.** "Exposed before success" only says the viewer saw an agent action
  before reaching the goal; some replay-day purchases were scripted to happen regardless. Only the arm comparison
  measures effect.
- **Exposure comes from simulator events.** `home_page_shown` with `page_action_id` and `message_delivered` with
  `action_id` are how the judge knows a viewer saw something; a production stream needs the same join keys.
- **No "continue watching" WhatsApp template.** `wa_new_episode_v2` ("Naya episode aa gaya") would be untrue for a
  series left weeks ago, so resume messages use the neutral `wa_recommend_title_v1`; templates cannot be invented.
- **Dialect names.** WhatsApp templates want a `dialect_name`; the pack has only codes, so the code is sent.
- **One replay day exercises some judgement only lightly.** Evidence confidence turned out to be a weak signal on
  this data (success rates barely differ by confidence), and almost no live open has evidence thin enough to fall
  below the 0.4 abstention line; only a handful of viewers were new enough for G03, so the mid-day G03 correction
  found no open G03 goals. I kept the thresholds principled rather than tuning them until they fire; the tests
  (`test_honesty.py`, `test_rules.py`) show each behaviour on viewers built for it.
- **Learning needs a judged earlier day.** The planner reads judged outcomes from the viewer's state. `run-all`
  starts fresh, so for the sample run nothing has been judged yet; `replay --continue-from` carries a judged day
  into the next one, and `tests/test_learning.py` shows a failed goal changing the next day's plan.
- **Single process.** The worker queue is simulated inside one process. State documents are versioned, so a second
  writer is refused rather than overwriting, but there is no distributed lock or real worker pool.
- **The demo app runs mock mode on localhost only.** Each what-if starts from the viewer's history and that day's
  events before the open; it is a window onto the agent, not a product screen.
