# Known limitations

What was left out on purpose within the 10-12 hour budget, and every assumption that the brief or the pack did
not settle. Each assumption is a setting in `config.py` or a named constant in `goals.py`.

## Assumptions

| Rule in the code | Status | Basis |
|---|---|---|
| A trigger buffered on the device for more than 10 minutes is not a live visit (`live_visit_max_age_s`) | assumption | the pack simulator ignores such sessions with the same 600 s cut-off; a page has a 120 min TTL, so acting on a days-old open helps nobody |
| "Recently lapsed" for G06 means within 30 days (`g06_recent_days`) | assumption | `goals.json` gives no number |
| G10 and G11 are never chosen | derived from data | G10 names no success event; `tv_app_open` never occurs in 142,906 events (checked at start-up, not hard-coded) |
| Priority when several goals are eligible: G01, G03, G02, G07, G04, G12, G06, G09, G08, G05 (`goal_priority`) | assumption | shortest windows and first-value goals first; engagement before monetisation. A consequence: G03 is rarely chosen because new viewers without a play get G01 |
| Page dialect is the app's dialect on the trigger; profile and deeplink are fallbacks for messages only | derived | `interventions.json`: the app requests the page of the dialect it is set to |
| Messages only for G07, G08 and G04, sent at 19:00 IST the next day, WhatsApp before push | design choice | proportionate actions; the brief asks for one channel and respects quiet hours |
| G06 success counts any `purchase_completed`, including the 1 rupee trial | assumption | `goals.json` says "any paid plan" |
| `decided_in_ms` includes simulated waits (10 s model timeout, 8 s store timeout, 2 s slow read) | design choice | the replay runs faster than real time; this keeps the latency report honest about dependency failures |
| Confidence = 0.4 + 0.15 profile + 0.1 history depth + 0.05 dialect agreement + 0.05 fresh trigger, minus 0.15 for dialect conflicts, 0.2 for a fallback, 0.1 for a veto | heuristic | the brief asks for a number with a reason; this is about evidence completeness, not the model's self-report |

## Left incomplete

- **Concurrency.** The replay is single-threaded and processes triggers in delivery order. The design shards by
  `user_id` (agents are independent, the store is the only shared state) but no worker pool is implemented.
- **Learning is recorded, not yet acted on.** Each viewer's state carries plain-language learnings from the judge.
  The next plan reconciles the active goal and the message cap, but does not yet change strategy based on past
  verdicts (for example, preferring a message after a page failed twice).
- **In-app messages** are supported in the action schema but never chosen; the simulator only queues them.
- **Dialect names.** The WhatsApp template wants a `dialect_name`; the pack has only codes, so the code is sent
  in upper case.
- **Live provider untested against a real API.** The OpenAI-compatible adapter is exercised with a fake client in
  the tests. The sample run is mock mode so evaluators can reproduce it without a key.
- **Exposure detection relies on simulator events.** `home_page_shown` with `page_action_id` and
  `message_delivered` with `action_id` are how the judge knows a viewer saw something; a production stream would
  need the same join keys.
- **Store degradation is simulated at the read layer** of our own SQLite store; the replay cannot make SQLite
  itself slow.
- **Sample size.** The agent-minus-holdout difference in the sample run has a confidence interval that includes
  zero. The report says so rather than claiming an effect.
- **The demo app is a local tool, not a product UI.** It runs the agent in mock mode only and serves on localhost.
  "Try the agent" starts each what-if from the viewer's 30-day history, so it ignores what the agent did for that
  viewer earlier on the replay day; the saved run is the record of that.
- **WhatsApp wording for resume goals.** The only approved template that names an episode is `wa_new_episode_v2`
  ("Naya episode aa gaya"). It is used to point at the viewer's next unwatched episode, which exists but is not new.
  A dedicated "continue watching" template would be more honest copy; templates cannot be invented.
