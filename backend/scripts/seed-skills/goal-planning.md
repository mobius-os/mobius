# Goal planning

Finish the read before material work on a new or resumed Goal. Bounded one-turn
work stays standard. Coordinators promote; delegated helpers never promote.
Honor owner opt-outs.

## The execution loop — read this first

A Goal is durable intent; execution attempts are separate. Resume its saved
record after interruption instead of replacing unfinished scope.

1. As coordinator, promote and record a small nested route. Parents verify;
   leaves work.
2. Run ready independent sibling leaves concurrently only when authorized and
   isolation avoids repeated input. Parallelism itself is not the saving. A
   helper files under the single running task; with several running, pass
   `spawn_agent` its `plan_task`.
3. Serialize dependencies, shared writes, plan revisions, and final integration.
4. Add discoveries beneath their owner; preserve unfinished siblings.
5. Work in-run. After verifying the outcome, call `update_goal(complete: true)`.

## Route and promote

Promote an observable outcome when durability helps across stages, turns,
discovery, parallel work, long operations, or restart risk and work can begin
now. This is judgment, not a keyword trigger. Recheck after owner choices,
implementation approval, or expanded scope.

Use the first-class `promote_goal(objective: "...", tasks: [...])` tool.
Use the script only when the tool is absent. A refusal is an answer; do not
retry it through the script:

```bash
python3 /data/platform/backend/scripts/mobius_control_mcp.py call promote_goal --args-json '{"objective":"Outcome and completion condition"}'
```

### Write Goal text for the owner

The objective is a visible heading, not an execution brief. Use one short,
plain-language outcome: “Restore clear Goal handoffs”, not a list of building,
testing, activation and contribution steps. Put those steps and their
completion conditions in the checklist; keep the full requested outcome.

Successful completion is a signal, `complete: true`, not another summary to
write. Verify first; keep useful evidence in task results or the chat
checkpoint rather than repeating it at completion. Communicate the outcome
once in your normal visible reply, including consequential limitations or
next actions; do not leave them only inside record details. Non-success
outcomes and deferrals still need their specific explanations.
Use normal sentences, spaces and paragraph breaks; never compress words
around numbers to save tokens. Existing records are not silently rewritten.

## Plan and work

Pass the plan to `promote_goal` as `tasks`, or send it later with
`update_goal`. Each task is `{id, title, depends_on?, parent_id?}`; a nested
task names its `parent_id`.

`update_goal` `tasks` edits the plan as one revision: a known id changes only
the fields you give, a new id adds a task. Finish one task and start the next
in the same call — `[{id: "inspect", status: "completed", result: "..."},
{id: "build", status: "running"}]`. Each call returns the revision and the
running and ready tasks, so there is no separate read. With no arguments it
returns the full plan: that is your overview. Your turn's brief shows other
tasks as id, title and status with `has_result`/`has_note` flags;
`read_goal(task=<id>)` expands any one task in full, with its ancestors'
constraints, children and prerequisite results. A helper's brief is its own
branch, and `read_goal` with no task re-reads it.

Statuses are pending, running, completed, blocked, failed, and cancelled;
notes and results hold up to 1000 characters. Keep evidence there and work
deepest leaves.
Children inherit ancestor dependencies and make a parent **Ready to verify**,
not complete. Verify upward; cancelled prerequisites are settled. Plans may
change; outcomes may not. Settled tasks do not close the Goal. To work on a
retained unfinished Goal, pass its exact `goal_id`; this attaches the current
attempt to the original objective and checklist, rather than creating a
replacement. Naming a held Goal deliberately reopens it: do that only when the
owner asked to continue that work. This needs a later owner-requested attempt;
an automatic result or an old recovery cannot undo a hold. If attachment is
refused, preserve the Goal rather than minting a replacement. An unrelated
follow-up must remain separate. Terminal outcomes cannot reopen. A legacy interruption without recorded intent
is unknown, not evidence that the owner paused the work.

### Responsibility, handoffs and outcomes

Work in-run; turns are not a budget. An unfinished Goal remains your
responsibility until a truthful outcome or real handoff. Do not end merely to
refresh context, select the next task, or ask the owner to discover Unpause.

- **Owner action:** give concrete instructions, explain the dependency, then
  save a question or approval card as your last action. Its answer resumes
  the same work. For a task the owner must perform elsewhere, choices such as
  **Done — verify and continue** and **I need help** make it actionable. A
  `blocked` task only records the gate; it is not a handoff. Do not offer
  `on_answer: "close"` for routine Goal blockers: that explicit no-reply choice
  pauses the card's Goal by owner choice, rather than continuing its work.
- **External work:** a durable Wait (read `waiting.md`) or a wake-enabled helper
  owns automatic continuation only when its actual condition/result can wake
  this chat. Never promise a wake for an unarmed condition or manual hold.
- **Deferred work:** an answered **Not now** defers that step, not every other
  authorized task. Record its reason, then continue independent work without
  retrying the declined action. When nothing useful can proceed, call
  `update_goal(defer: 'What was deferred and why', tasks: [...])` and end normally.
  This records **On hold**, preserves the original outcome and checklist, and
  releases this Goal's claims without calling them completed. It does not stop
  your reply or require another question. Resolve existing helpers, questions
  and undelivered Waits before holding; do not abandon a real handoff. A later
  explicit owner continuation resumes the same Goal. Never use deferral to
  conceal a crash, claim completion, or decide on the owner's behalf to stop.
- **Completed:** verify the original promised outcome. Update obsolete internal
  steps explicitly with a reason, without cancelling unmet requirements to
  fake success. Call `update_goal(complete: true, finished_claims: ["WORK_KEY"])`
  with only held work keys for exact actions performed; include final `tasks`
  edits if needed.
- **Cannot complete:** when the original outcome is genuinely unreachable,
  explain the obstacle, what you tried, partial results and what is missing.
  First seek an actionable owner choice on a saved card—access, a feasible
  alternative, accepting the limitation, or calling off the work. Keep the
  Goal open while that decision is outstanding. A temporary approval gate or
  outage is not capitulation. After that choice, record `cannot_complete:
  {reason: '...', efforts: '...', unmet_outcome: '...'}`; never silently narrow
  the objective or mark unachieved requirements successful.
- **Cancelled:** when the owner calls off or redirects the outcome, reconcile
  unfinished tasks honestly with reasons and record `cancel: 'Owner called off:
  reason'`. Cancellation is not successful completion.

Task edits and an outcome settle in one atomic revision: if refused, none is
saved. No outcome closes while helpers are active; non-success outcomes retain
explicit unmet-task reasons rather than converting them to green. A settled
checklist alone does not close the Goal.

A clean execution accidentally ending without an outcome, card, registered
wake, or explicit hold gets one targeted settlement continuation in the
existing runner. It must not redo verified work, invent questions, narrow
scope, or burn unlimited turns. If that pass also fails to hand off or settle,
a visible technical recovery failure preserves the Goal; it is not **Cannot
complete**. Resource and restart recovery retain this bound. Continue
interrupted work when the owner asks or the platform sends an authorized
continuation. Stop, owner holds, and arbitrary crashes never grant automatic
continuation.

Never settle a Goal by stopping the chat: that presses this chat's own Stop,
ends your run at once and cuts off anything after it. Stop remains a
recoverable interruption, distinct from owner cancellation of the outcome.
Only when the owner explicitly asks you to stop, summarize, then honor it last with
`mapi -X POST /api/chat/stop -d "{\"chat_id\":\"$CHAT_ID\"}"`.

## One shared nested checklist

A Goal plan describes work, not agents or execution attempts. Create a task for a
meaningful outcome; several reviewers or retries may work on the same task.
Helpers assigned to a task can use `update_goal_tasks(tasks=[...])` to add and
update substeps within that branch, then file children under those steps with
`spawn_agent(plan_task=...)`. Prefix new task ids with the branch id to avoid
collisions. Helpers without a filed assignment have no checklist-write scope.

No plan-approval exchange is needed. Batch related checklist updates; they
update the shared state and UI without waking the parent. Send messages only
when a decision, blocker, finding or result changes another agent's work. Read
task details on demand instead of copying full plans into prompts or messages.

The assigning parent accepts its helper's boundary task after reviewing the
normal final report; the helper leaves that task unfinished, but may verify and
complete work beneath it. Only the coordinator settles the overall Goal. This
reuses ordinary result delivery, not another approval protocol.
