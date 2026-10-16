# Impact report

Question: did viewers whose agent was allowed to act reach their goal more often than viewers whose agent
chose a goal but was not allowed to act (the randomized holdout)?

## Headline: agent arm vs holdout arm (intention to treat)

Every triggered viewer got a goal from the same rules; only the agent arm got actions. One row per viewer
(first goal of the replay day). Inconclusive goals (window still open at the end of the data) are excluded
from the rate but shown.

| Arm | Viewers | Judged | Succeeded | Inconclusive | Success rate |
|---|---|---|---|---|---|
| agent | 757 | 757 | 88 | 0 | 11.6% |
| holdout | 746 | 746 | 70 | 0 | 9.4% |

Difference (agent minus holdout): **+2.2 pp**, 95% interval -0.9 pp to +5.3 pp.
The interval includes zero: at this sample size the data does not show that the agent changed the outcome. That is the honest reading, not a failure of the measurement.

## By goal

| Goal | Agent judged | Agent rate | Holdout judged | Holdout rate | Difference |
|---|---|---|---|---|---|
| G01 | 59 | 37.3% | 51 | 29.4% | +7.9 pp |
| G02 | 224 | 15.2% | 206 | 13.1% | +2.1 pp |
| G03 | 1 | 0.0% | 0 | n/a | n/a |
| G05 | 35 | 5.7% | 41 | 7.3% | -1.6 pp |
| G06 | 7 | 0.0% | 6 | 16.7% | -16.7 pp |
| G07 | 391 | 4.3% | 395 | 3.0% | +1.3 pp |
| G08 | 28 | 32.1% | 36 | 25.0% | +7.1 pp |
| G09 | 11 | 27.3% | 8 | 25.0% | +2.3 pp |
| G12 | 1 | 100.0% | 3 | 33.3% | +66.7 pp |

Small per-goal counts mean wide intervals; read the by-goal rows as direction, not proof.

## Why the simpler comparison misleads

Inside the agent arm one can compare viewers who actually saw a personalised page or message with those
who did not. That is the comparison the pilot report made (served vs everyone else) and it is biased:
a viewer is only *exposed* if they open the app again, and viewers who open the app again are exactly the
viewers more likely to play, complete or pay anyway.

| Agent-arm viewers | Judged | Success rate |
|---|---|---|
| exposed to an agent action | 667 | 12.4% |
| not exposed (no action, or published but never seen) | 90 | 5.6% |

The gap above mixes the agent's effect with self-selection. The arm comparison does not, because the
holdout was assigned at random before anyone opened the app.

## The earlier pilot, re-read the same way

The internal report compared served viewers (217, 40.1% reached the goal) with everyone
else (3674, 17.7%) and called it a 2.3x uplift. Using the pilot's own randomized arms:

| Pilot arm | Viewers | Reached goal |
|---|---|---|
| treatment | 1912 | 18.3% |
| holdout | 1979 | 19.6% |

Difference: -1.3 pp. The headline uplift was selection, not effect.

## Attribution detail (agent arm, all goals)

| Attribution | Goals |
|---|---|
| exposed_no_success | 584 |
| exposed_before_success | 83 |
| published_but_never_seen | 47 |
| no_action_taken | 42 |
| success_before_exposure | 1 |

`exposed_before_success` is the only bucket where the agent could have caused the outcome; even there
the arm comparison, not the exposure, is the evidence of effect.

Data: 1505 judged goals from `outcomes.jsonl`; outcome events from the pack simulator
(seed `oua-practice`, 32052 events). Evaluators re-run with their own seed and replay day.
