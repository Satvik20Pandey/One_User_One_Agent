# Final report: sample replay

Replay day 2026-10-01 IST, 2769 triggers from 1746 viewers, mock model provider.

## What the agents did

| Status | Decisions |
|---|---|
| HOLDOUT_NO_ACTION | 1325 |
| PUBLISHED | 924 |
| NO_ACTION | 430 |
| DUPLICATE | 90 |

Actions published: 938 home_page, 65 whatsapp, 117 push, 29 in_app_message.

Goals committed (both arms): G08 467, G02 344, G07 228, G09 131, G06 115, G01 110, G05 76, G12 35, G04 2, G03 1.

### Why the agent did nothing

| Reason | Decisions |
|---|---|
| holdout | 1035 |
| buffered offline | 424 |
| still live | 154 |
| no catalogue content | 91 |
| duplicate | 90 |
| dialect unknown | 48 |
| no measurable goal | 3 |

## Real-time behaviour

- Every decision published within the 60 s budget: 2769/2769; late 0, failed 0.
- p50 2 ms, p95 1205 ms, max 18001 ms, including mock model latency and 752 decisions that waited on a slow dependency.
- 4 decision workers share one queue: queue wait p95 0 ms, max 0 ms. Peak load 79 opens in one minute (2026-10-01T04:45 UTC).
- Duplicate triggers recognised: 90. Fallbacks: 8 model_unavailable, 1 store_degraded.
- Model: 741 calls, advice used 732, rejected 1, provider failures 8, not asked because it had nothing to add 218, skipped during a traffic surge 50.

## Changed conditions noticed during the day

- 2026-10-01T04:45:09.279Z to 2026-10-01T04:56:45.713Z traffic surge: 242 opens in 10 minutes against 14.5 per 10 minutes in the hour before; optional model calls were skipped.
- 2026-10-01T15:04:01.555Z to 2026-10-01T15:14:39.439Z traffic surge: 166 opens in 10 minutes against 35.8 per 10 minutes in the hour before; optional model calls were skipped.
- 2026-10-01T07:00:00.000Z title T0001 withdrawn: 24 live pages republished without it, 0 scheduled messages cancelled.
- 2026-10-01T08:30:00.000Z success rule for G03 corrected: 0 open goals re-tagged; all G03 goals judged under the corrected rule (version goals.json@2026-09-30+G03@2026-10-01T08:30:00.000Z).
- 2026-10-01T10:35:00.000Z model_provider down until 2026-10-01T10:42:00.000Z: every model call fails with HTTP 503.
- 2026-10-01T13:10:00.000Z user_data_store degraded until 2026-10-01T13:17:00.000Z: p95 read latency 8 s and 20% of reads time out.
- Catalogue descriptions that read like instructions to an AI, flagged and ignored: T0002, T0522. Descriptions are never sent to the model; the titles themselves remain normal content.
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
- Pages accepted 938, shown personalised 832, published but not shown 13.
- Push delivered 101/117 (invalid tokens 16), WhatsApp delivered 65/65, clicks 2.

## Outcomes

| Arm | Succeeded | Failed | Inconclusive |
|---|---|---|---|
| agent | 173 | 585 | 0 |
| holdout | 149 | 601 | 1 |

The table counts goals (1509). A viewer can hold more than one goal over the day, one at a time; 1501 viewers had at least one.

Intention-to-treat success rate, first goal per viewer: agent 22.9% (757 viewers) vs holdout 19.9% (743 viewers), difference +2.9 pp (95% interval -1.2 pp to +7.1 pp). See `impact_report.md`.

## Where to look

- `decisions.jsonl`: one record per trigger with goal, action or reason for none, evidence, conflicts, checks, timing.
- `actions.jsonl`: what was published, in the simulator's input format.
- `outcomes.jsonl`: verdict and attribution for every committed goal.
- `viewer_state/<user_id>.json`: everything the agent knew, decided and observed for one viewer.
- `latency_report.json`, `replay_summary.json`, `sim/simulator_report.json`.
