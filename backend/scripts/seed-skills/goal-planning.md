# Goal planning

Read this before material work on every ordinary prompt that delegates an
outcome, and whenever a Goal is created or resumed. It turns genuinely durable
work into the visible, dependency-aware todo list above the composer. Questions
and explanation-only prompts are not delegated outcomes and do not need this
routing read. Automatic promotion belongs to a top-level owner turn; a delegated
subagent keeps its assigned task bounded rather than starting another Goal.

## Decide whether the request should become a Goal

The working agent already has the request, context, and constraints; do not add
a second classifier call. Automatically create a Goal only when all of these
are true:

- The partner delegated an outcome to complete, not merely a question,
  explanation, recommendation, critique, or investigation to report back.
- Completion has an observable condition the agent can eventually verify.
- Durability materially helps because the route likely spans multiple
  independently verifiable stages or turns, repeated work, dynamic discovery,
  safe parallel branches, a long-running operation, or a plausible restart.
- The task can make meaningful progress without first waiting for a material
  owner choice, destructive approval, or an external event.

Make this decision before the first material tool call for **every** ordinary
top-level delegated outcome. Do not silently skip it because the work looks
easy enough to finish in one turn or is described as synthetic, a fixture, or
a test; those labels say nothing about durability. Several independently
verifiable stages followed by an explicit final verification are strong
evidence that durability materially helps, even when each individual action is
simple. This remains a structural judgment using all criteria above, not a
keyword trigger.

### Recheck when the work changes phase

Goal routing is a transition check, not a one-time classification of the first
message. Reapply every criterion **before the first material action** when any
of these boundaries is crossed, even inside the same agent turn:

- the partner answers a required scope, direction, or approval question;
- investigation or critique turns into an implementation request;
- a bounded task reveals a durable multi-stage branch, merge surprise, or
  substantially broader scope; or
- a later instruction adds independently verifiable outcomes to work already
  underway.

An unanswered question may correctly prevent promotion because meaningful work
cannot begin yet. Its answer removes that blocker; it is not a reason to keep
the newly approved implementation ordinary. Likewise, discovery can make a
request Goal-shaped after several standard steps. Promote at that boundary
before editing or launching the expanded branch—do not wait for another user
message and do not rewrite completed work as if the Goal existed earlier.

Do not infer a Goal from message length, words such as “thoroughly” or
“production-ready,” a multi-file edit, or the mere fact that a useful task list
could be written. Keep bounded work that can honestly finish in one turn as a
standard prompt. Use schedules for recurring background work. Honor “just
answer,” “one pass,” and “don't make this a Goal” as explicit opt-outs.

Examples:

- **Goal:** “Migrate every caller to the new API, update the documentation, and
  keep working until the complete test suite passes.”
- **Goal:** “Address all issues you discover in this checkout flow, including
  substantial nested work, then run a production-readiness audit.”
- **Standard prompt:** “Explain why this failed and recommend what to do.”
- **Standard prompt:** “Add a loading state to this button and test it.”

When the criteria hold, promote the current physical turn before material work.
Call the platform's first-class `promote_goal` tool when it is available; its
single `objective` argument is the concise outcome and observable completion
condition. The helper is resilience, not an equivalent convenience path: use
it only when the tool is absent from the callable inventory or an attempted
tool call returns a failure:

```bash
python3 /data/platform/backend/scripts/goal_promote.py \
  'Concise outcome and observable completion condition'
```

Run the helper as its own command. The tool and helper attach the already-running
turn to Möbius's provider-neutral Goal state and verify the committed identity;
neither adds a hidden message nor starts a second turn. Do not call a
provider-private `create_goal` from an ordinary turn, queue a synthetic `/goal`,
or claim activation before the operation succeeds. Explicit `/goal` remains
authoritative.

## Decide whether the Goal earns a plan

Keep a one-step Goal unplanned. Create a plan early when the objective has two
or more independently verifiable outcomes, explicit ordering, repeated work,
or independent branches that can proceed in parallel. This is agent judgment,
not a keyword parser: do not manufacture busywork merely to fill a list.

The Goal remains the stable outcome. Tasks describe the current route to it and
may be revised as evidence changes, but revising the route never authorizes
silently changing the requested outcome.

When discovery adds work, run `show`, then replace the complete plan with
`set --tasks-json`, preserving every existing task's truthful status, note, and
progress while adding or rewiring only the newly understood route.

## Publish the dependency graph

Use the platform helper; it reads `$CHAT_ID`, `$API_BASE_URL`, and
`$AGENT_TOKEN` itself:

```bash
python3 /data/platform/backend/scripts/goal_plan.py set \
  --task 'inspect|Inspect the owning path' \
  --task 'build|Implement the change|inspect' \
  --task 'verify|Verify the real behavior|build'
```

Each task is `id|visible title|comma-separated dependencies`. Omit the final
field for a root task. Independent roots are ready together; a dependent task
becomes ready only after every named dependency completes. For richer state
(initially completed work, notes, or repeated progress), use `--tasks-json`.

Translate common shapes directly:

- `A then B then C` -> `A`, `B|A`, `C|B`.
- `A and B, then C` -> root tasks `A` and `B`, then `C|A,B`.
- `A three times sequentially` -> one task with
  `"progress":{"current":0,"total":3}` and advance it after each run. Do
  not mark it complete until progress is `3/3`.

## Grow the route as the work teaches you more

The initial plan is a useful route, not a prediction ritual. When completing a
task reveals substantial independently verifiable work, add it as a child of
the task that owns the newly discovered requirement:

```bash
python3 /data/platform/backend/scripts/goal_plan.py add inspect-auth \
  'Inspect the authentication boundary' \
  --parent audit \
  --completion-condition 'The owner and child authority paths are verified'
```

Children may themselves gain children. The deepest ready leaves are the work
that can run now; safe independent leaves may run in parallel. After all direct
children settle, their parent becomes **Ready to verify** rather than
auto-completing. Verify the parent's own completion condition, record the
concise result, and only then complete it. This repeats upward until the root
outcome is proved.

A parent owns coordination, not descendant transcripts. Pass each child only
the bounded contract and context it needs. Fold its concise result into its
immediate parent; the top-level agent needs the status and verified result of
its direct children, not every lower-level implementation detail.

## Keep the visible state truthful

Before starting a task, mark it running; after evidence proves its outcome,
mark it completed. Use blocked for missing owner/external input and failed for
a real terminal failure:

```bash
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status running
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status completed
python3 /data/platform/backend/scripts/goal_plan.py update repeat-check \
  --status running --progress 2/3
python3 /data/platform/backend/scripts/goal_plan.py show
```

The platform rejects cycles, missing dependencies, premature dependent starts,
and incomplete repeated tasks marked complete. It computes ready/waiting state;
do not duplicate that scheduler with prose or timers.

When several ready tasks are independent, run them in parallel only when the
available delegation capability and write isolation make that safe. Shared
live writes—including Goal-plan revisions—still need one serialized integrator;
parallel read/review work is the easy case. When a delegated task settles, fold
its evidence into the parent, update the matching task, and start newly ready
work. Ordinary in-turn fleets still die with the turn; use a capability that
explicitly owns durable child work when the task must survive a restart.

Immediately before completing the native Goal, run the mediated completion
preflight as its own command:

```bash
python3 /data/platform/backend/scripts/goal_plan.py check-complete
```

For a planned Goal it rejects unfinished tasks and active mapped delegations.
A green todo list supports the completion audit; it never replaces checking the
actual result. Where an explicit provider Goal exposes mediated `update_goal`,
call it with `status: complete` only after the preflight and real completion
audit pass; otherwise the verified end of the agent turn completes the Goal.
