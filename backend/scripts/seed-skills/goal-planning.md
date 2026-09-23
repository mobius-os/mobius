# Goal planning

Finish the read before material work on a new or resumed Goal. Bounded one-turn
work stays standard. Only the owner promotes; honor opt-outs.

## The execution loop — read this first

A Goal is durable intent; execution attempts are separate. Resume its saved
record after interruption instead of replacing unfinished scope.

1. Promote, then record a small nested route. Parents verify; leaves work.
2. Run ready independent sibling leaves concurrently only when authorized and
   isolation avoids repeated input. Parallelism itself is not the saving.
3. Serialize dependencies, shared writes, plan revisions, and final integration.
4. Add discoveries beneath their owner; preserve unfinished siblings.
5. Work in-run; checkpoint before a handoff. After verifying the outcome, run
   `goal_plan.py complete --result 'Verified evidence'`.

## Route and promote

Promote an observable outcome when durability helps across stages, turns,
discovery, parallel work, long operations, or restart risk and work can begin
now. This is judgment, not a keyword trigger. Recheck after owner choices,
implementation approval, or expanded scope.

Use the first-class `promote_goal` tool. The helper is resilience, not an
equivalent convenience path: use it only when the tool is absent or an attempted
tool call returns a failure:

```bash
python3 /data/platform/backend/scripts/goal_promote.py 'Outcome and completion condition'
```

## Plan and work

```bash
python3 /data/platform/backend/scripts/goal_plan.py set \
 --task 'inspect|Inspect' --task 'build|Build|inspect' --task 'verify|Verify|build'
python3 /data/platform/backend/scripts/goal_plan.py add child 'Check edge' --parent inspect
python3 /data/platform/backend/scripts/goal_plan.py update inspect --status running
```

Tasks are `id|title|dependencies`. Work deepest leaves. Children inherit ancestor
dependencies and make a parent **Ready to verify**, not complete. Verify upward;
cancelled prerequisites are settled. Plans may change; outcomes may not.

`show` includes Goal status and the plan; settled tasks do not close the Goal.
Before replacing a plan, `show` it and preserve results. Use `goal_plan.py list`,
`show --goal-id ID`, and `goal_plan.py resume ID` for retained obligations.
Resume attaches an ordinary attempt; it cannot reopen closed work.

### Make every unfinished wait explicit

Before ending unfinished, create exactly one owning interaction: for an observable
condition, read `waiting.md` and declare a durable Wait; for owner action, use the
saved question tool, which keeps the Goal marked **Waiting for you**. Restart uses
its dedicated card.

With no gate, keep working. Terminal settlement continues the exact Goal only
when its saved plan advanced during the admitted turn; otherwise it asks the
owner. An unchanged plan is not progress.

Work in-run; turns are not a budget. Use `goal_plan.py context` for current focus
or `context --task ID` for a branch. Running tasks select focus; the view includes
parent requirements and dependencies. `show` reads the full plan. Do not end a
run merely to refresh context or select the next task. Never end with “tell me
when…”, prose status, a bare paused Goal, or a custom status card.

Before an unfinished handoff:

```bash
python3 /data/platform/backend/scripts/goal_plan.py checkpoint \
 --summary 'Verified progress and remaining obligations' --next-action 'Exact next step'
```

After verifying the original outcome:

```bash
python3 /data/platform/backend/scripts/goal_plan.py complete --result 'Verified evidence'
```

`complete` validates and records completion; no separate preflight is required.
`goal_plan.py check-complete` is an optional read-only task diagnostic, not
completion. A green plan or ended attempt leaves the Goal open. After owner
Stop, summarize and run `goal_plan.py stop` last.
