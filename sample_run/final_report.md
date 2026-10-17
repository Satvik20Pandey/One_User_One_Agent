# Final report: sample replay

Replay day 2026-10-01 IST, 2769 triggers from 1746 viewers, mock model provider.

## What the agents did

| Status | Decisions |
|---|---|
| HOLDOUT_NO_ACTION | 1325 |
| PUBLISHED | 924 |
| NO_ACTION | 430 |
| DUPLICATE | 90 |

Actions published: 939 home_page, 62 whatsapp, 110 push, 29 in_app_message.

Goals committed (both arms): G08 467, G02 344, G07 191, G09 131, G06 115, G05 113, G01 110, G12 35, G04 2, G03 1.

### Why the agent did nothing

| Reason | Decisions |
|---|---|
| holdout | 1035 |
| buffered offline | 424 |
| still live | 153 |
| duplicate | 90 |
| no catalogue content | 56 |
| goal G02 chosen but the open carried no app dialect, so the  | 20 |
| goal G08 chosen but the open carried no app dialect, so the  | 18 |
| goal G01 chosen but the open carried no app dialect, so the  | 17 |
| goal G06 chosen but the open carried no app dialect, so the  | 10 |
| goal G07 chosen but the open carried no app dialect, so the  | 9 |
| goal G05 chosen but the open carried no app dialect, so the  | 4 |
| no measurable goal | 3 |
| goal G12 chosen but the open carried no app dialect, so the  | 3 |
| goal G09 chosen but the open carried no app dialect, so the  | 2 |
| goal G04 chosen but the open carried no app dialect, so the  | 1 |

## Real-time behaviour

- Every decision published within the 60 s budget: 2769/2769; late 0, failed 0.
- p50 2 ms, p95 1207 ms, max 18001 ms, including mock model latency and 769 decisions that waited on a slow dependency.
- 4 decision workers share one queue: queue wait p95 0 ms, max 0 ms. Peak load 79 opens in one minute (2026-10-01T04:45 UTC).
- Capacity, using this day's measured decision times: the whole day at 3x traffic gives 1 worker(s) p95 wait 253 ms, 2 worker(s) p95 wait 0 ms, 4 worker(s) p95 wait 0 ms; 1,000 opens in one minute gives 4 workers max wait 18,725 ms (0 over budget), 16 workers max wait 0 ms (0 over budget), 32 workers max wait 0 ms (0 over budget).
- Duplicate triggers recognised: 90. Fallbacks: 8 model_unavailable, 1 store_degraded.
- Model: 758 calls, advice used 749, rejected 1, provider failures 8, not asked because it had nothing to add 202, skipped during a traffic surge 50.

## Changed conditions noticed during the day

- 2026-10-01T04:45:09.279Z to 2026-10-01T04:56:45.713Z traffic surge: 242 opens in 10 minutes against 14.5 per 10 minutes in the hour before; optional model calls were skipped.
- 2026-10-01T15:04:01.555Z to 2026-10-01T15:14:39.439Z traffic surge: 166 opens in 10 minutes against 35.8 per 10 minutes in the hour before; optional model calls were skipped.
- 2026-10-01T07:00:00.000Z title T0001 withdrawn: 24 live pages republished without it, 0 scheduled messages cancelled.
- 2026-10-01T08:30:00.000Z success rule for G03 corrected: 0 open goals re-tagged; all G03 goals judged under the corrected rule (version goals.json@2026-09-30+G03@2026-10-01T08:30:00.000Z).
- 2026-10-01T10:35:00.000Z model_provider down until 2026-10-01T10:42:00.000Z: every model call fails with HTTP 503.
- 2026-10-01T13:10:00.000Z user_data_store degraded until 2026-10-01T13:17:00.000Z: p95 read latency 8 s and 20% of reads time out.
- Catalogue descriptions that read like instructions to an AI, flagged and ignored: T0002, T0522. Descriptions are never sent to the model, and these titles are held back from pages and messages until a person reviews them.
- Plan revisions triggered by live events: 11 (purchases that closed an open goal before its deadline).

## Scripted model faults

| Viewer | Scripted fault | What happened |
|---|---|---|
| U-01991 | unsupported_claim | model asked; answer rejected: model claimed goal 'G04' but no event supports eligibility (eligible: ['G05', 'G07']); answer discarded |
| U-02509 | unsupported_claim | model not asked: trigger is 0d 15h 19m old (buffered offline); the viewer is not in the app now |
| U-04860 | unsupported_claim | model not asked: trigger is 0d 9h 28m old (buffered offline); the viewer is not in the app now |
| U-02563 | malformed_output | model not asked (not needed: the hero and the copy were decided by evidence or an approved template); PUBLISHED |
| U-01090 | timeout | model not asked: viewer is in the randomized holdout arm |

Every fault type is also forced through the full agent in `tests/test_failures.py`, so each one is exercised even when the agent has no reason to ask the model in the replay.

## Simulator verdict on the published actions

- Policy problems: **0** (holdout, consent, quiet hours, frequency cap).
- Pages accepted 939, shown personalised 832, published but not shown 13.
- Push delivered 96/110 (invalid tokens 14), WhatsApp delivered 62/62, clicks 3.

## Outcomes

| Arm | Succeeded | Failed | Inconclusive |
|---|---|---|---|
| agent | 174 | 584 | 0 |
| holdout | 149 | 601 | 1 |

The table counts goals (1509). A viewer can hold more than one goal over the day, one at a time; 1501 viewers had at least one.

Intention-to-treat success rate, first goal per viewer: agent 23.0% (757 viewers) vs holdout 19.9% (743 viewers), difference +3.1 pp (95% interval -1.1 pp to +7.2 pp). See `impact_report.md`.

## Where to look

- `decisions.jsonl`: one record per trigger with goal, action or reason for none, evidence, conflicts, checks, timing.
- `actions.jsonl`: what was published, in the simulator's input format.
- `outcomes.jsonl`: verdict and attribution for every committed goal.
- `viewer_state/<user_id>.json`: everything the agent knew, decided and observed for one viewer.
- `latency_report.json`, `replay_summary.json`, `sim/simulator_report.json`.
