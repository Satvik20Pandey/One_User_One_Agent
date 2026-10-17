# Impact report

Question: did viewers whose agent was allowed to act reach their goal more often than viewers whose agent
chose a goal but was not allowed to act (the randomized holdout)?

## Headline: agent arm vs holdout arm (intention to treat)

Every triggered viewer got a goal from the same rules; only the agent arm got actions. One row per viewer
(first goal of the replay day). Inconclusive goals (window still open at the end of the data) are excluded
from the rate but shown.

| Arm | Viewers | Judged | Succeeded | Inconclusive | Success rate |
|---|---|---|---|---|---|
| agent | 757 | 757 | 173 | 0 | 22.9% |
| holdout | 744 | 743 | 148 | 1 | 19.9% |

Difference (agent minus holdout): **+2.9 pp**, 95% interval -1.2 pp to +7.1 pp.
The interval includes zero: at this sample size the data does not show that the agent changed the outcome. That is the honest reading, not a failure of the measurement.

### Who is counted

- 1509 goals were judged in total for 1501 viewers. 8 viewer(s) got a second goal the same
  day after the first closed early (the viewer stopped qualifying); the table above counts the first goal only.
- Triggered viewers: agent 863, holdout 883. Viewers without any goal: agent 106, holdout 139. Nearly all of them only had app opens that were
  buffered offline for hours or days, which the same rule treats as "not a live visit" in both arms.

## By goal

| Goal | Agent judged | Agent rate | Holdout judged | Holdout rate | Difference |
|---|---|---|---|---|---|
| G01 | 59 | 37.3% | 51 | 29.4% | +7.9 pp |
| G02 | 182 | 14.8% | 159 | 13.2% | +1.6 pp |
| G03 | 1 | 0.0% | 0 | n/a | n/a |
| G04 | 1 | 0.0% | 1 | 0.0% | +0.0 pp |
| G05 | 35 | 5.7% | 40 | 7.5% | -1.8 pp |
| G06 | 52 | 9.6% | 63 | 14.3% | -4.7 pp |
| G07 | 118 | 2.5% | 108 | 0.9% | +1.6 pp |
| G08 | 229 | 36.2% | 237 | 28.7% | +7.6 pp |
| G09 | 69 | 33.3% | 60 | 31.7% | +1.7 pp |
| G12 | 11 | 72.7% | 24 | 50.0% | +22.7 pp |

Small per-goal counts mean wide intervals; read the by-goal rows as direction, not proof.

## Why the simpler comparison misleads

Inside the agent arm one can compare viewers who actually saw a personalised page or message with those
who did not. That is the comparison the pilot report made (served vs everyone else) and it is biased:
a viewer is only *exposed* if they open the app again, and viewers who open the app again are exactly the
viewers more likely to play, complete or pay anyway.

| Agent-arm viewers | Judged | Success rate |
|---|---|---|
| exposed to an agent action | 640 | 25.3% |
| not exposed (no action, or published but never seen) | 117 | 9.4% |

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
| exposed_no_success | 479 |
| exposed_before_success | 162 |
| published_but_never_seen | 73 |
| no_action_taken | 42 |
| success_before_exposure | 2 |

`exposed_before_success` is the only bucket where the agent could have caused the outcome; even there
the arm comparison, not the exposure, is the evidence of effect.

Data: 1509 judged goals from `outcomes.jsonl`; outcome events from the pack simulator
(seed `oua-practice`, 31813 events). Evaluators re-run with their own seed and replay day.
