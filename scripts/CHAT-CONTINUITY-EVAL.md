# Agent-authored continuity evaluation

This is a behavior evaluation, not just a schema or substring test. A saved
checkpoint is necessary evidence, not proof that it contains the right facts.
Never report dry runs or mocked tests as live Claude/Codex results.

## Comparison boundary

The pre-change source is `7b8ea22c1d3a21294a959ef8894d5f9156f85094`. Its
`backend/scripts/chat_note.py` uses new transcript material where its cursor
matches, but regenerates the cumulative note through a separate provider CLI.
Its provider handoff additionally synthesizes the running note plus the full
visible transcript. Keep that revision fixed for comparison; do not reintroduce
its publisher as a production feature flag or change a live owner's deployment
back and forth merely to obtain a baseline.

Preferred baseline: run the same fixture scenarios on that revision in an
operator-provided isolated instance BEFORE candidate activation, or replay the
recorded candidate visible transcript through that revision's isolated old
publisher and clearly label this **summary-only replay**, not an end-to-end A/B.
Never copy or inspect live credential files to provision a baseline. Use the
deployment/provider's supported authentication path. If a comparable baseline
cannot be run safely, state that limitation rather than invent a comparison.

## Approval and isolation

- Live runs consume provider usage. Obtain the agreed live-test budget before
  `--execute`; expanding a short smoke into the full matrix is a separate cost
  decision when not already covered. A dry run never makes network calls.
- Use newly created, explicitly selected-model fixture chats titled
  `Continuity eval: ...`. Their first turn must begin AFTER the candidate's
  constitution/runtime activation; existing chats retain immutable old prompts.
- The collector takes an explicit empty chat ID and checks provider/model. It
  never changes owner defaults, creates an app, copies auth, or deletes history.
  Beware: the ordinary owner chat model-PATCH also changes future-chat defaults.
  Configure fixture models using an isolated instance or an owning test setup
  that accounts for that side effect, not an unexamined model-PATCH loop.
- Drive one provider/fixture at a time. Record other active work; global memory
  is not attributable to this fixture when other chats/builds are active.
- Test fixtures cannot modify platform/apps/settings or contact outsiders.
  The single-turn coding fixture can write only its own temporary directory.

## Run ladder

```bash
# No server or paid calls:
python3 scripts/chat-continuity-eval.py --scenario short

# After approval and fixture preparation (exact selected model required):
python3 scripts/chat-continuity-eval.py --execute \
  --label candidate --scenario short --chat-id FIXTURE_UUID \
  --provider codex --model EXACT_PICKER_MODEL
```

1. Short, 3 turns, once on Claude and once on Codex. Inspect before expanding.
2. Medium, 10 turns, both providers, adding corrected assumptions, supplied
   evidence, blockers and changed requirements.
3. Long, 30 turns, both providers. Revisit early constraints after substantial
   subsequent work; verify explicit supersession, not contradictory accumulation.
4. `toolburst`, one genuinely tool-using turn, both providers. Check intermediate
   checkpoints after a reproduced failure, repair and verified tests, not only
   a final save. Record source/test artifacts and actual outcomes.
5. Additional isolated cases: manual owner title, topic change, tool-free reply,
   retry after a lost receipt, a stale concurrent save, abrupt interruption,
   owner-input pause, native compaction, portable compact, and switches in BOTH
   directions. Native compaction and Möbius portable handoff are distinct tests.

The collector records runtime state, checkpoint revision changes, visible chat,
and usage for each turn. In baseline mode it also waits for the old post-turn
publisher's source cursor before sending the next turn, saves its note, and
samples the post-turn memory window. This avoids falsely measuring the old
system with its summarizer still running or skipped by the next turn.

The collector does not automatically answer unexpected cards or retry ambiguous
sends. An incomplete result names its fixture for inspection; that chat may
still be running and must be settled/stopped deliberately before more work.

## Rubric (human/agent review against source evidence)

For every meaningful event, record the first checkpoint that contains it and
whether the next dependent action preceded that checkpoint. Judge semantic
content, not a particular phrase. Facts in test prompts are supplied evidence;
facts from executed tests must be tied to the corresponding actual tool result.

Required facts in the shared Juniper scenarios:

- Offline/no network; never mutate original files; retain `0042` as a string.
- Semicolon replaces comma; quoted delimiters work.
- Medium: preserve order/duplicates, UTF-8, all-or-nothing import, decimal
  arithmetic with display-only rounding, unresolved date convention.
- Long: ISO dates and negative refunds resolved; preview 10 supersedes 5;
  missing amount is not zero; preserve quoted whitespace and source coordinates;
  exact header mapping; duplicates invalid; no export scope creep; undecided
  size limit remains open. `TX-019` is an example, not a universal default.
- Supplied fixture results are not misreported as the agent's own verified work.
- No implementation/deployment claimed for the design-only scenarios.

Mark omissions, unsupported additions, stale state, premature success, duplicate
entries and needless title churn separately. Recover a fresh agent from the
saved continuity and uncovered transcript, then ask it to continue real next
work. Mere recollection when the original full session is still present does
not establish successful compaction or handoff.

Evaluate warm-context and missing-context behavior separately. A normal save
must not return the existing name, paragraph, or recent journal entries. An
agent that still knows the current state/revision should not reread it at every
milestone. When context is actually missing, verify that it recovers the state
before changing it rather than guessing. When only a fresh source cursor is
needed, `after_revision` should avoid replaying already-seen entries. Count
redundant reads and repeated continuity bytes alongside missed updates; shorter
prompts are an improvement only when continuation quality is preserved.

## Coverage and failure tests

- Checkpoint mid-turn, then add more assistant text/steered owner input: handoff
  must contain the unsaved tail. A count of mutable assistant rows is not a
  trustworthy coverage cursor.
- Omit a checkpoint in one turn, then save an unrelated delta in the next:
  source coverage must NOT silently jump over the missing interval. Only an
  explicit agent acknowledgment of the read's `source_cursor`, after catching
  up substantive uncovered information, may advance the saved boundary.
- Change an earlier covered message: the verified whole-prefix digest must
  invalidate coverage and include the full source rather than omit it.
- Interrupt after a checkpoint but before final answer; verify saved facts and
  raw uncovered messages survive. Do not force the old agent to resume solely
  to make a missing checkpoint look successful.
- Retry identical checkpoint ID/payload: one entry. Reuse with changed content
  or stale expected revision: explicit conflict, no lost update.
- Preserve full legacy note bytes as a historical baseline. No invented dates
  or false assertion that old narrative was originally an append-only log.
- Active/waiting/idle is derived from actual runtime, with a snapshot timestamp,
  not inferred from the generated paragraph or used as a permanent truth.
- Very large journals remain available. If portable handoff invokes bounded
  synthesis, record its calls/cost separately; never call that zero-model work.

## Resource accounting

Compare peak and settled container charge, process PSS (avoid shared-RSS double
counting), transient summary workers, time to first answer, time to durable
checkpoint, and time until housekeeping ends. Sample without browsers/builds.
Keep raw RAM and reclaimable cache distinct. Short polling can miss a peak, so
label measurements sampled rather than absolute maxima.

Report cached/uncached input, output, model/effort and calls. Chat usage counters
may NOT include the old out-of-band summary CLI, so a missing baseline counter
is unknown, never zero. Collect that provider usage separately if available;
otherwise report the measurement gap and compare observed prompt/output sizes
without claiming monetary savings. More expensive working-agent output and
extra checkpoint tool round-trips can offset some token savings.

## Coaching and holdouts

Fork the exact failed fixture session through the existing coaching helper when
authorized (`backend/scripts/fork_chat.py`), not an invented replacement seeded
with an excerpt. Ask which instruction/tool affordance caused the omission or
overwriting behavior and how to make the intended update natural. Coaching must
not mutate the fixture or give its fork owner control.

Change the common prompt/tool instructions based on observed failures, keeping
examples short. Do not add semantic validators, periodic nagging, forced extra
turns, or a background evaluator to production. Re-run a NEW holdout scenario
(different domain, names, corrections and ordering) on both providers. Report
first-attempt and coached results separately. Passing these samples is evidence,
not a guarantee that agents can never omit a checkpoint.

## Current evidence

The collector's hermetic tests and dry run do not spend provider usage. Live
results belong in a dated local evaluation report with exact source revisions,
fixture/session IDs, models, prompt versions, traces and reviewer findings.
Do not mark the implementation Goal complete until the agreed live matrix,
continuation tests and coaching/holdout loop have actually been exercised.


## Codex-first release boundary

The initial release is qualified by observed Codex behavior, not by universal
checkpoint compliance. Short planning/correction/blocker holdouts with the
explicit closeout checklist updated the current paragraph and journal; a real
portable compaction followed by a fresh Codex session preserved the decisions
and accepted new work. Longer diagnostic trajectories retained early and late
constraints but exposed stale short paragraphs, motivating the coached wording.

A shorter, consolidated version of the instructions failed a new three-turn
Codex holdout (no checkpoints). Keep the evaluated explicit read/save/receipt
checklist; equivalent prose is not evidence of equivalent model behavior.
Intermediate checkpoints within a single coding turn remain unreliable even
with explicit guidance. This is an instruction-following limitation, not an
atomic-write guarantee. Missing coverage conservatively retains source text.

Claude Opus evaluation is deferred pending provider availability. Native
provider compaction, live cross-provider switching, and a matched old-runtime
resource A/B are not established by the portable Codex recovery test. Do not
claim a total-container RAM target or guaranteed token savings from it.

Existing chats keep immutable pre-change prompt snapshots, even after a server
restart or compaction. They may not autonomously use the new tools; use a new
chat for the coached behavior. Legacy notes and uncovered transcript remain
available, but the retired background publisher does not keep those old notes
fresh. No silent prompt rewrite or fallback summary model is introduced.
