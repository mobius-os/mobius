# Goal planning

Read this before material work on every delegated outcome and whenever a Goal
starts or resumes. **This read is a serial gate:** finish it before batching any
investigation, fixture read, edit, or other material call. Do not run them
concurrently. Apply it for **every** ordinary top-level delegated outcome.
Questions stay standard; only the top-level owner promotes.

## The execution loop — read this first

1. Route before the first material action; recheck at phase changes.
2. Promote, then plan. A Goal is durable intent, not an executor.
3. Inspect ready leaves; parallelize safe independent work. Serialize shared
   writes, Goal-plan revisions, and the final integrator.
4. Finish in-turn or create one real handoff. Delegated children return future
   conditions to the parent; they never own a cross-turn Wait.
5. Reconcile evidence, verify the outcome, and run `goal_plan.py check-complete`.

## Route and promote

Promote only for a delegated observable outcome when durability materially
helps (multiple stages/turns, repetition, discovery, parallel branches, a long
operation, or restart risk) and work can start without an owner/approval/event
gate. This is structural judgment, not a keyword trigger. Synthetic/test work
gets the same decision. Keep bounded one-turn work standard and honor opt-outs.

### Recheck when the work changes phase

Reapply before the first material action after an owner choice, investigation
becoming implementation, discovery of a durable branch, or added outcomes. An
unanswered owner question can defer promotion. Its answer removes that blocker.
Promote before the new branch; never backdate work.

Use the first-class `promote_goal` tool. The helper is resilience, not an
equivalent convenience path: use it only when the tool is absent or an
attempted tool call returns a failure.

```bash
python3 /data/platform/backend/scripts/goal_promote.py 'Outcome and completion condition'
```

Never use a provider-private Goal or synthetic `/goal`. Explicit `/goal` wins.

## Plan and work

Leave only a genuinely one-step Goal unplanned. Otherwise plan immediately:

```bash
python3 /data/platform/backend/scripts/goal_plan.py set \
 --task 'inspect|Inspect' --task 'build|Build|inspect' --task 'verify|Verify|build'
```

A task is `id|title|dependencies`; independent roots are ready together. After
every update, inspect ready leaves. Parallelism is a readiness decision, not a
helper quota. Serialize shared work and integration. Add discovered work under
its owning task:

```bash
python3 /data/platform/backend/scripts/goal_plan.py add child 'Check edge' --parent inspect
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status running
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status completed
```

Work deepest leaves. Children make a parent **Ready to verify**, not complete.
Verify upward. Keep child contracts bounded. Plans may change; outcomes may not.

### Make every unfinished wait explicit

A paused task is bookkeeping, not a handoff. Before ending unfinished, create
exactly one owning interaction:

- Observable condition: top-level parent reads `waiting.md` and declares the
  durable Wait; never leave a poller or prose promise.
- Owner action: use the real question tool; its card keeps the Goal marked
  **Waiting for you**.

Do not end with “tell me when…”, a bare paused Goal, or a custom status card.

Before completion run:

```bash
python3 /data/platform/backend/scripts/goal_plan.py check-complete
```

It rejects unfinished work/delegations but never replaces verifying reality.
