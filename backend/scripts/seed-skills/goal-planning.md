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
5. Work in-run. After verifying the outcome, run `complete`.

## Route and promote

Promote an observable outcome when durability helps across stages, turns,
discovery, parallel work, long operations, or restart risk and work can begin
now. This is judgment, not a keyword trigger. Recheck after owner choices,
implementation approval, or expanded scope. Use the first-class `promote_goal`
tool. `goal_promote.py 'Outcome'` (same directory as `G`) is resilience, not an
equivalent convenience path: use it only when the tool is absent or an
attempted tool call returns a failure.

## Commands

`G` is `python3 /data/platform/backend/scripts/goal_plan.py`; type the full path
(shell variables do not persist). This is the whole set; skip `--help`.

```bash
G set --task 'a|Inspect' --task 'b|Build|a' --task 'c|Verify|b' --start a
G update a --status completed --result 'evidence' --start b  # finish + start
G update x y --status cancelled                              # several ids
G update b --progress 2/5 --note 'text'
G add a2 'Check edge' --parent a --depends-on b
G update b --status completed --next-action 'Exact next step'  # handoff
G context --task ID; G context; G show; G list; G resume ID; G stop
```

Tasks are `id|title|deps`. Each write prints `Goal plan revision N: x/y
complete. Running: … Ready: …`, so skip `show`. A stale-revision race is retried
once; a refused multi-step write says what applied. `no active Goal to plan`
means promote, or `list` then `resume ID`.

Statuses: pending, running, completed, blocked, failed, cancelled. Work deepest
leaves. Children inherit ancestor dependencies and make a parent **Ready to
verify**, not complete. Cancelled prerequisites are settled. Plans may change;
outcomes may not. Before `set` on an existing plan, `show` it and keep results.
Resume attaches an ordinary attempt; it cannot reopen closed work.

### Make every unfinished wait explicit

Before ending unfinished, create exactly one owning interaction: for an observable
condition, read `waiting.md` and declare a durable Wait; for owner action, use the
saved question tool, which keeps the Goal marked **Waiting for you**. Restart uses
its dedicated card. A button in an app or the Changes panel is not a handoff,
and a `blocked` task only records the gate: when only the owner can unblock it
(an approval, a choice, or a change of scope), put exactly that on the card.

With no gate, keep working. Terminal settlement continues the exact Goal only
when its saved plan advanced during the admitted turn and still has runnable
work; otherwise it asks the owner. An unchanged plan is not progress; any real
plan change is, so do not checkpoint to record it. Only before a handoff, add
`--next-action` to your last `update` (or `G checkpoint --next-action '…'`) to
leave the next attempt its next step.

Work in-run; turns are not a budget. Use `context --task ID` for a branch. Do not
end a run merely to refresh context or select the next task. Never end with
“tell me when…”, prose status, a bare paused Goal, or a custom status card.

After verifying the original outcome:

```bash
G complete --result 'Verified evidence'
```

It validates and records completion; no separate preflight is required.
`goal_plan.py check-complete` is an optional read-only task diagnostic, not
completion. A green plan or ended attempt leaves the Goal open. After owner
Stop, summarize and run `G stop` last.
