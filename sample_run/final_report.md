# Final report: sample replay

Replay day 2026-10-01 IST, 2769 triggers from 1746 viewers, mock model provider.

## What the agents did

| Status | Decisions |
|---|---|
| HOLDOUT_NO_ACTION | 1325 |
| PUBLISHED | 909 |
| NO_ACTION | 445 |
| DUPLICATE | 90 |

Actions published: 926 home_page, 206 push, 147 whatsapp.

Goals committed (both arms): G07 786, G02 430, G01 110, G05 77, G08 65, G09 19, G06 13, G12 4, G03 1.

### Why the agent did nothing

| Reason | Decisions |
|---|---|
| holdout | 1030 |
| buffered offline | 422 |
| still live | 166 |
| no catalogue content | 101 |
| duplicate | 90 |
| dialect unknown | 48 |
| no measurable goal | 3 |

## Real-time behaviour

- Every decision published within the 60 s budget: 2769/2769; late 0, failed 0.
- p50 2 ms, p95 5 ms, max 18001 ms (16 decisions waited on a degraded dependency).
- Peak load 79 triggers in one minute (2026-10-01T04:45 UTC, the offline-buffer burst).
- Duplicate triggers recognised: 90. Fallbacks: 11 model_unavailable, 1 store_degraded.
- Model: 909 calls, advice used 897, rejected 1, provider failures 11.

## Changed conditions noticed during the day

- 2026-10-01T07:00:00.000Z title T0001 withdrawn: 24 live pages republished without it, 0 scheduled messages cancelled.
- 2026-10-01T08:30:00.000Z success rule for G03 corrected: 0 open goals re-tagged; all G03 goals judged under the corrected rule (version goals.json@2026-09-30+G03@2026-10-01T08:30:00.000Z).
- 2026-10-01T10:35:00.000Z model_provider unavailable until 2026-10-01T10:42:00.000Z: every model call fails with HTTP 503.
- 2026-10-01T13:10:00.000Z user_data_store degraded until 2026-10-01T13:17:00.000Z: p95 read latency 8 s and 20% of reads time out.
- Plan revisions triggered by live events: 3 (purchases that closed an open goal before its deadline).

## Simulator verdict on the published actions

- Policy problems: **0** (holdout, consent, quiet hours, frequency cap).
- Pages accepted 926, shown personalised 832, published but not shown 14.
- Push delivered 176/206 (invalid tokens 30), WhatsApp delivered 147/147, clicks 6.

## Outcomes

| Arm | Succeeded | Failed | Inconclusive |
|---|---|---|---|
| agent | 87 | 670 | 0 |
| holdout | 70 | 678 | 0 |

The table counts goals (1505). A viewer can hold more than one goal over the day, one at a time; 1503 viewers had at least one.

Intention-to-treat success rate, first goal per viewer: agent 11.5% (757 viewers) vs holdout 9.4% (746 viewers), difference +2.1 pp (95% interval -1.0 pp to +5.2 pp). See `impact_report.md`.

## Where to look

- `decisions.jsonl`: one record per trigger with goal, action or reason for none, evidence, conflicts, checks, timing.
- `actions.jsonl`: what was published, in the simulator's input format.
- `outcomes.jsonl`: verdict and attribution for every committed goal.
- `viewer_state/<user_id>.json`: everything the agent knew, decided and observed for one viewer.
- `latency_report.json`, `replay_summary.json`, `sim/simulator_report.json`.
