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
- 55 tests on a hand-made mini pack cover duplicate and late triggers, conflicting sources, model and store
  failures, the corrected goal rule, the verifier's vetoes, injected catalogue text, the judge, the CLI and the
  demo app (driven headlessly with Streamlit's test runner, including live agent runs with failures switched on).
- Reports are generated from program output. I did not type a number into a document.

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
| G07 takes half the goals at a 4% success rate | true | Kept as a documented choice; KNOWN_LIMITATIONS says what it costs and what I would change |

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
