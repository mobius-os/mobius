# Goal planning

Read this when creating or resuming a Goal. Finish the read before material
work. Questions and honestly bounded one-turn outcomes stay standard; only the
top-level owner promotes.

## The execution loop — read this first

1. On resume, inspect the persisted plan instead of creating a replacement.
2. Promote, then record the smallest useful nested route. A Goal is durable
   intent; parents coordinate and verify, while leaves do the work.
3. Run ready independent sibling leaves concurrently when isolated contexts
   avoid repeating input. Give each helper a bounded brief and require a compact
   result. Parallelism itself is not the saving.
4. Serialize dependencies, shared writes, plan revisions, and final integration.
5. Grow or revise the plan only when discovery changes the route or the owner
   expands the outcome. Nest new work under its owner; do not append history.
6. Finish in-turn or create one real handoff, then verify and run
   `goal_plan.py check-complete`.

## Route and promote

Promote only for a delegated observable outcome when durability materially
helps (multiple stages/turns, repetition, discovery, parallel branches, a long
operation, or restart risk) and work can start without an owner/approval/event
gate. This is structural judgment, not a keyword trigger. Synthetic/test work
gets the same decision. Keep bounded one-turn work standard and honor opt-outs.

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

A task is `id|title|dependencies`; independent siblings are ready together.
After every update, inspect ready leaves. Add discovered work under its owning
task:

```bash
python3 /data/platform/backend/scripts/goal_plan.py add child 'Check edge' --parent inspect
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status running
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status completed
```

Work deepest leaves. Children inherit their ancestors' dependencies. Children
make a parent **Ready to verify**, not complete; verify upward. A cancelled
prerequisite is settled and must not strand dependants. Plans may change;
outcomes may not silently change.

### Make every unfinished wait explicit

A paused task is bookkeeping, not a handoff. Before ending unfinished, create
exactly one owning interaction:

- Observable condition: top-level parent reads `waiting.md` and declares the
  durable Wait; never leave a poller or prose promise.
- Owner action: use the real question tool; its card keeps the Goal marked
  **Waiting for you**.

If no actor owns the next move, the platform may give the same Goal one compact
repair turn to reconcile its plan or establish an owner. A repeated miss stays
visibly paused and resumable; task count or progress never buys more turns.
Do not end with “tell me when…” or a custom status card.

Before completion run:

```bash
python3 /data/platform/backend/scripts/goal_plan.py check-complete
```

It rejects unfinished work/delegations but never replaces verifying reality.
After it succeeds, finish the current turn normally; the platform derives the
Goal's completed presentation from that settled turn and complete plan. Do not
call a provider-private Goal tool or search for a second completion endpoint.
On a resumed continuation, inspect the existing Goal instead of promoting a
paraphrased replacement.
