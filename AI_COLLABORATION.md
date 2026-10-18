# AI collaboration log

I used Claude Code (Claude Fable 5.1) as the main assistant and ChatGPT as a second reviewer of the plan. This
file records what they did, how I checked their output, and what I rejected.

## How the work was split

| Step | AI did | I did |
|---|---|---|
| Understanding | Read the brief and every pack file; profiled the data (duplicates, buffered triggers, dialect conflicts, `d7` with no content, missing success events, the pilot's selection bias) | Decided what mattered: goal choice must be rule-based, holdout must get a shadow goal, the pack stays out of the repo |
| Plan | Drafted the module list and a requirements-vs-assumptions table | Had ChatGPT review it; cut the module count, added milestone gates with real output, made the live model optional and last |
| Code | Wrote the modules and tests in milestones | Ran each milestone myself, read the decision records, and sent back what was wrong (below) |
| Docs | Drafted README, ARCHITECTURE, KNOWN_LIMITATIONS and this file | Checked every number against the generated reports; rewrote wording I could not defend |

## How output was checked

- Every milestone ran before the next started: pack counts against the pack README, one viewer end to end with
  the decision JSON printed for six representative triggers, the full replay, then the pack simulator.
- The simulator is the referee the brief gives us: the final run reports zero policy problems (no holdout,
  consent, quiet-hour or frequency-cap violations) and every published page accepted.
- 90 tests on a hand-made mini pack cover duplicate and late triggers, conflicting sources, model and store
  failures, the corrected goal rule, the verifier's vetoes, injected catalogue text, the judge, the CLI and the
  demo app (driven headlessly with Streamlit's test runner, including live agent runs with failures switched on).
- Reports are generated from program output. The few figures quoted in the design notes come from earlier runs
  and say so.

## What I rejected or changed

- **The model choosing the goal.** The first planner let the model switch between eligible goals. I rejected it:
  holdout viewers never call the model, so the arms would have had different goal assignment and the impact
  comparison would be biased. Goal choice is now rules only; the model advises on the hero title and push copy.
- **Confidence that was always 0.95.** The first formula saturated. It now starts lower and is about evidence
  completeness, so a fresh, fully-evidenced decision lands at 0.75.
- **A planner that relied on the verifier.** The first full replay showed 174 messages vetoed on the daily cap and
  89 pages vetoed as "still live". Correct outcome, sloppy planner. The agent now checks both before planning
  and records a clean reason; the verifier still checks independently (tests cover both).
- **Unavailable titles in the "next episode" row.** The verifier caught 19 pages carrying a title whose licence had
  ended. The row builder now filters the viewer's unfinished series by availability.
- **A brand-new viewer's own open counted as evidence.** Found by a test: a viewer with no profile and no history
  was offered a goal because the trigger had been stored first. "Known" now means evidence beyond the open itself.
- **Degraded-store cost charged at 8 s on every read.** The control event says p95 8 s with 20% timeouts, so a
  timed-out read costs 8 s and a slow successful one 2 s.
- **Anthropic as the live provider.** I had chosen it first; I switched to an OpenAI-compatible adapter so one
  adapter covers OpenRouter, Groq and OpenAI, and kept mock mode as the only thing evaluators need.
- **A static demo page.** My first demo was a generated HTML file. It only displayed results, so I replaced it
  with a small Streamlit app where a reviewer can run the real agent and break things on purpose. The app imports
  the same agent, rules and verifier; no decision logic is copied into the UI. I kept it to four tabs and asked for
  no React or extra services.
- **Report counts that did not reconcile.** A review noticed 1,505 judged goals but 1,503 viewers in the impact
  table. Tracing the records showed two holdout viewers with a second goal after the first closed early. The
  reports now state both counting units instead of leaving the reader to guess.
- **ChatGPT's suggestion to drop SQLite for JSON files.** Rejected: idempotency by `event_id` and "find every open
  G03 goal" at the rule correction need an index, and four small tables are easier to explain than a hand-rolled
  one.

## A cold review before submitting

I gave a fresh AI session only the brief, the pack and this repository, with no notes from me, and asked it to
grade the work as a hiring panel would. I checked each finding in the code and the outputs before acting:

| Finding | What I found | What I did |
|---|---|---|
| The verifier reused the planner's facts, so it could not disagree on eligibility or consent; the docs said otherwise | true | It now re-reads the store and derives consent with separate code; the docs say exactly what is and is not independent |
| 139 WhatsApp messages said "Naya episode aa gaya" about episodes that were not new | true, and I had only documented it | Resume messages use the neutral template; the verifier vetoes "new episode" copy for old titles |
| Re-running the judge stacked duplicate learnings, visible in the shipped sample | true | Judge learnings are replaced on every run; a test judges twice |
| A live event closed a goal by its type, without checking the condition (a trial purchase would close G05) | true | Goals close early only when the event really meets the success condition |
| Opens from phones with fast clocks were treated as live even when buffered for hours | true for 2 opens; wrong that anything was published (both viewers were holdout) | The phone's own send-minus-occur gap is used whatever its clock says, as the simulator does |
| Instructions hidden in catalogue text were avoided but never noticed | true | The catalogue is scanned at load; flagged titles appear in the run summary |
| G07 takes half the goals at a 4% success rate | true: 455 of those viewers were subscribers who also qualified for a new-release or habit goal | Goal order now follows the viewer's lifecycle stage; subscribers get habit or new-release goals, free viewers keep "resume" |
| Confidence never changes what the agent does | true | A push or WhatsApp now needs evidence confidence of 0.6; below that the agent publishes the page only |
| Learnings are written but never read | true | The planner reads judged outcomes and does not retry a failed goal for seven days |
| The model is called on every decision | true | It is asked only when there is copy to write or a hero the evidence did not decide, and skipped during traffic surges or when the budget is short |
| No queue or concurrency model, so "traffic triples" is never exercised | true | Opens now wait for one of four workers, the wait counts against the 60 s budget, and a surge monitor caught both the offline burst and the evening peak |
| Evidence refs show their fact's time, not their own | true | Each ref carries its own time and age; each decision stores its full fact table |
| In-app messages are never used | true | Viewers without push or WhatsApp consent can get one in-app message a day |

The second pass found one more bug of its own: the final report crashed on a control event type it did not know.
Fixed, and the unseen-day test now runs through the reports.

A second cold review (again with no notes from me) found smaller things, which I checked and fixed:

| Finding | What I did |
|---|---|
| During a data-store outage a days-old buffered open could still get a page | Freshness is checked before the outage fallback |
| A purchase closed the goal but left its upsell message scheduled | A closing goal cancels the messages it scheduled |
| "Just released" was used for titles up to 30 days old; "trial ending" ignored when the trial ends | Copy states the real release age; trial-ending only when the trial ends within 3 days (checked again by the verifier) |
| Eligibility assumptions (like the 30-day "recently lapsed") were missing from decision records | They are copied onto every decision |
| The G03 correction was applied to G03 goals that had already ended | Corrections apply only to goals still open at correction time |
| Abstention was almost entirely mechanical | Not enough evidence (confidence below 0.4) is now a recorded no-action |
| The mock model only echoed the rules | It now picks the most popular allowed title in the viewer's top genre, so advice is accepted or rejected for real |
| Learning never ran end to end | `replay --continue-from` runs day two on day one's judged state; a test shows the change. Building it exposed an id collision when continuing a store, also fixed |
| Flagged titles were still promoted | They are held back until a person reviews them |
| No statement of how big an effect the test could detect | The impact report now gives the minimum detectable effect |

A third cold review found one bug my own checks had missed, because a later change undid an earlier fix: once the
model was asked for push copy, its hero pick could replace a hero the evidence had chosen, so about 19 resume-goal
pages lost the very series they were about. The model can now only fill a hero the evidence left open, the old hero
stays on the page, and model copy may not add numbers or drop the title. The same review found smaller issues, all
fixed: a misleading "no catalogue content" reason when the app sent no dialect, new-release judging that counted
titles released after the goal started, `StaleState` raised but not handled (a stale write is now retried on fresh
state), availability checked when the open arrived rather than when the page goes live, and stale report text. The
lesson I took: after every AI-proposed change, re-run the end-to-end checks of earlier fixes, not only the new tests.

A fourth review found the most important remaining bug: when an open arrived without a dialect (common on login
events), the agent fell back to the profile's dialect even though the viewer's own app had shown a different one
minutes earlier, so a handful of messages went out in the wrong dialect. It now uses the app setting from the
viewer's latest event, keeps the disagreement visible (which also holds the message back), and the verifier checks
every message against the app's latest setting with its own code. The same review showed "return after absence"
goals being closed by the very return they were waiting for; goals are no longer abandoned because their own
progress changed their entry facts, and the verifier agrees with an open active goal from its own read of the store.

A fifth review found nothing broken and a few smaller points, which I fixed: push and in-app copy were English
even for Hindi apps (they now follow the app's language, like the approved WhatsApp templates), an open that sent
"NA" as its dialect now counts as missing evidence and holds back push and WhatsApp, learning only uses verdicts
whose window has already ended, and holdout records no longer carry assumptions about pages that were never
published. Two points I kept as documented limits rather than code: a live page is not retracted when its goal
closes, and publish times vary by a few milliseconds between runs because they include measured processing time.

## Where AI output was wrong and I only noticed by running it

A multi-file shell command Claude wrote to create test fixtures failed on the shell parser; it rewrote the
fixtures as plain files. The first judge export skipped the one viewer decided while the store was unavailable;
it now exports viewers from the decision log as well. The `run-all` command, the one evaluators will use, read a
flag that only `replay` defines and crashed on first use; a CLI test now drives every command. The judge crashed
on an empty outcome stream. Building the demo page exposed a real agent bug: for "resume a series" goals the page
hero and the message promoted the most recently played series, not the one the goal was about and would be judged
on. The content builders now take the goal's own series, and a test pins it. The first version of the app's live
tab crashed on the second click: Streamlit serves each interaction on a new thread and SQLite refused the cached
connection. The app test caught it before I opened a browser. Finally, because the evaluators use their own
simulator seed and a second replay day, I re-ran the pipeline with four other seeds and a shifted day: one seed
crashed the judge on the weekly-habit goal (two plays on the deciding day), the final report printed a hard-coded
date, and an unknown scripted model fault would have crashed the mock provider. All three are fixed and tested.
All of these were caught by running or looking at the output, not by reading the code.
