# Orchestrating fleets

Field lessons for designing a multi-helper orchestrated run — a fleet of helpers fanned out from one run script with verify and synthesis stages. `Read` this before authoring any such run. These are lessons, not a procedure: the plan for each run is designed fresh around that job's shape, and if the job does not resemble the runs these lessons came from, set them aside.

When a run teaches a new lesson or contradicts one below, update this file — it earns its keep only while it matches the evidence.

---

## Design the plan fresh; reuse the judgment

An attempt to template these runs failed on real evidence: three scripts written for the *same recurring intent* (reconciling an upstream merge) shared almost no reusable structure — different phases, different fan-out, different verification — because the useful structure came from per-run investigation of that day's actual conflict. Re-derive the plan every time. What transfers is the judgment below, not the script.

## Lessons that held across runs

- **Verifiers must try to refute, not confirm.** Friendly checkers wave everything through. Prompt the verifier with "Try to REFUTE this; default to refuted if uncertain." In one analysis run this killed 4 of 6 plausible-sounding proposals — every refutation cited files the proposer had not read.
- **Give different verifiers different lenses** — does-the-problem-earn-the-machinery, technical feasibility under the real runtime, correctness, does-it-reproduce — rather than N copies of one skeptic. Diverse lenses catch failure modes redundancy cannot.
- **Demand evidence over testimony.** Tell every judge to check claims against the actual files and live state, and every finder to pin findings to something re-checkable (file and line, a command and its output). An unpinned finding cannot be verified and should not survive.
- **Front-load verified ground truth.** Scout inline first, then put the verified facts into each helper's brief. Helpers that re-derive shared context arrive at slightly different versions of it, and the differences masquerade as findings.
- **Each helper's brief must stand alone.** Helpers start with no context. A short, curated brief beats pasted history: the task, the ground truth, the exact return shape (use a schema), and nothing else.
- **The fleet dies with the turn.** Deliver the fleet's result inside the turn that launched it, or do not launch it. Never end a turn promising a report from a fleet that cannot outlive it. If a fleet is killed mid-run, a bare re-run of the same script starts from zero — every launch gets a fresh run id. Recovery is resume-by-run-id (the id is in the launch result) and works only within the same session; across sessions, read the helper transcripts and author a continuation.
- **Say what was dropped.** If the run bounds coverage — top-N, sampling, a helper that never reported — state that in the synthesis. Silent truncation reads as "covered everything."

## What failed, so you don't retry it

- Templating whole run scripts (above): the structure does not transfer, only the judgment.
- Confirm-biased verification: single friendly verifiers approved findings that refute-framed panels later killed with evidence.
- Promising post-turn delivery from an in-turn fleet.

## Real examples

Past run scripts — with their phase skeletons, verifier prompts, and schemas — persist on this instance under `$CLAUDE_CONFIG_DIR/projects/*/*/workflows/scripts/*.js`. Consult one concrete prior example when designing a new run, especially a review or verification fleet. The Workflows app shows how each recorded run actually went, including helpers that never reported.
