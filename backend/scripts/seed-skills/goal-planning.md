# Goal planning

Use this whenever a native `/goal` is created or resumed. It turns a genuinely
multi-step Goal into the visible, dependency-aware todo list above the composer.
Read it before doing material work on an active Goal that has multiple
verifiable stages, repeated work, or stages that can safely run in parallel.

## Decide whether the Goal earns a plan

Keep a one-step Goal unplanned. Create a plan early when the objective has two
or more independently verifiable outcomes, explicit ordering, repeated work,
or independent branches that can proceed in parallel. This is agent judgment,
not a keyword parser: do not manufacture busywork merely to fill a list.

The Goal remains the stable outcome. Tasks describe the current route to it and
may be revised as evidence changes, but revising the route never authorizes
silently changing the requested outcome.

Tasks may also form an ownership tree. A parent owns only its immediate
children and re-checks its own completion condition after they settle. It does
not need descendant transcripts or implementation detail; concise child
status, blocker, result, and evidence are enough. A child can apply the same
rule recursively to work it discovers.

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

When material work is discovered during execution, add it rather than hiding
it in a note. `--parent` makes it a direct child and
`--completion-condition` records what its owner must verify:

```bash
python3 /data/platform/backend/scripts/goal_plan.py add repair-migration \
  'Repair the legacy migration' --parent backend \
  --completion-condition 'Old and fresh databases both migrate cleanly'
```

Children may run in parallel when their dependency lists permit it. Settled
children make their parent **ready to verify**; they never auto-complete it.

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
live writes still need one serialized integrator; parallel read/review work is
the easy case. When a delegated task settles, fold its evidence into the parent,
update the matching task, and start newly ready work. Ordinary in-turn fleets
still die with the turn; use a capability that explicitly owns durable child
work when the task must survive a restart.

Durable delegated children may use the same guarded Subagents helper for their
own immediate children, up to the platform's bounded depth. This is local
orchestration, not transcript propagation: X reports to B, B verifies itself
and reports to A. Use stable task keys at every level so restart attachment is
idempotent. Never switch to a provider-native agent CLI or an ordinary detached
process to bypass this ownership boundary.

Immediately before marking the native Goal complete, run the mediated
completion preflight as its own command:

```bash
python3 /data/platform/backend/scripts/goal_plan.py check-complete
```

Only call `update_goal` with `status: complete` when that command exits zero
and the actual requested outcome has been achieved. The preflight allows a
deliberately unplanned one-step Goal; for a planned Goal it rejects pending,
running, blocked, or failed tasks. Cancelled tasks are treated as deliberately
removed from the route. A green todo list supports the completion audit; it
never replaces checking the actual result.
