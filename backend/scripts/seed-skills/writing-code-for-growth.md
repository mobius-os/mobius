---
name: writing-code-for-growth
description: Use for durable code changes—features, fixes, refactors, schemas, and tests—even when design quality is not mentioned. Reduce future complexity with deep interfaces, explicit outcomes, earned extension seams, and intent-named tests. Skip throwaway probes and experiments.
---

# Writing code for growth

Use this as the close-to-the-code companion to the project's conventions.
The goal is not merely working code, but code that leaves the next change
cheaper to understand and make.

Apply these principles through the repository's existing language,
architecture, and conventions. Do not introduce a preferred pattern merely
because this skill names it. Improve the owning design in its native
vocabulary.

Skip this skill for genuinely throwaway work: a debug probe, one-shot script,
or experiment that will not survive the session.

## Start from the user-visible contract

Before a structural change, state the action's ordinary meaning, the state
owner that may participate, and one outcome that must never happen. When two
plausible readings would change the meaning of a named action, resolve that
choice before building; preserving the action's existing meaning is the safe
default.

A persistence fix must not change what an action opens or reuses. If it starts
choosing that, stop and re-check the contract before hardening its edge cases.
For New, Create, Reset, and equivalent actions, add a negative test proving
that state from another entity cannot leak into the result.

### Treat existing guards as evidence

When a change collides with a test, contract, security boundary,
data-preservation rule, or documented performance invariant, inspect the
guard's name, rationale, owning contract, and relevant history before editing
it. Decide whether it checks incidental implementation or protects deliberate
behavior.

Updating an incidental assertion while preserving equivalent behavior is
ordinary maintenance. If the deliberate invariant itself would change, do not
weaken or remove the guard merely to obtain a passing build. Explain the
conflict and user-visible impact, offer safe alternatives, and ask the owner
before changing it. When implementation changes but the contract does not,
preserve or generalize the regression test.

## 1. Reduce complexity where it lives

Complexity grows through obscurity and dependency spread. Before adding a
parameter, branch, helper, wrapper, type, or setting, ask whether it removes
more complexity than it introduces.

When a fix feels awkward, zoom out. Find the layer that owns the behavior and
change the seam there rather than adding a retry, timer, early-return guard, or
special case around the symptom. If a workaround is unavoidable, record the
root cause and the condition under which the workaround can be removed.

Prefer one coherent mechanism over compatibility shims or parallel paths,
except where compatibility protects real data or an external contract.

## 2. Prefer deep modules and narrow seams

A useful module exposes a small, understandable interface while absorbing
substantial complexity behind it. Callers should not need to know the
implementation's incidental decisions.

Wrappers and pass-through helpers must earn their place. They are useful when
they isolate I/O, narrow an untyped boundary, provide semantic stability,
enforce access policy, or own observability. A function that only repeats
another function's signature and forwards its arguments is usually another
surface to learn, not an abstraction.

Keep one boundary where one will do. Avoid stacks of adapters that all express
the same transition.

## 3. Choose the lightest honest shape

Reuse a shape already flowing through the code when the new work only passes
it along. Name a contract when it crosses modules, has multiple meaningful
fields, or would otherwise require distant callers to remember positions or
magic keys.

Use the language's lightest natural construct:

- a plain value or collection for local data;
- a named data shape when its fields form a durable contract;
- a structural interface or protocol when multiple implementations are
  present or explicitly next;
- a behavior-owning class when state and operations genuinely belong
  together;
- inheritance only when implementations share meaningful behavior, not just a
  method name.

Do not create a new type merely to rename an existing result. Do not leave an
opaque tuple or undocumented mapping to act as a cross-module protocol.

Let access patterns choose data structures. Optimize for the operations the
code actually performs, while keeping the simpler representation when the
scale and frequency do not justify extra machinery.

## 4. Design ordinary absence and failure without special cases

Prefer representations that let the normal path handle ordinary empty or
inactive states:

- empty collections when "nothing here" is still a valid collection;
- a no-op implementation when doing nothing is valid behavior;
- an optional value when absence is meaningful and callers must acknowledge
  it;
- a default factory or immutable default when a real default is appropriate.

Choose the representation that tells the truth; do not apply null objects or
optionals mechanically.

Make significant outcomes visible at the boundary that owns them. When work
can partially succeed, return or emit a structured outcome the caller can
inspect rather than hiding errors in mutable receiver state. Keep result
shapes uniform when callers process them uniformly.

Avoid boolean parameters that select between distinct behaviors already known
by the caller. Prefer intent-named operations or a policy object when the
variation is real.

Validate at genuine boundaries—user input, configuration, persistence,
plugins, concurrency, and external systems. Within code you control, prefer
types, tests, and clear structure over guards whose only purpose is to police
another developer.

## 5. Keep knowledge local and discoverable

Put the information needed to understand a change near the code that uses it.
When non-local behavior is necessary, leave a searchable sign at both ends
through a precise name, type, or short rationale.

Names are interfaces. Prefer names that state the domain action or invariant
over generic names such as `process_data` or `handle_event`. A future reader
should be able to find the right seam with symbol search before reading the
whole module.

Comments should preserve what the code cannot express: why an invariant
exists, why an apparent simplification is unsafe, or when a workaround can be
removed. Do not restate the next line, refer to the current task, or reassure
the reader that code is correct.

Use the repository's established documentation location for module seams and
extension guidance. Add new documentation structure only when the existing
one cannot make the contract discoverable.

## 6. Use tests as durable intent

Tests should preserve the behavior and the reason it matters. Name them so a
future reader searching for the old concept, failure mode, or domain action
can find the replacement.

Prefer:

`test_idempotent_retry_does_not_repeat_the_side_effect`

over:

`test_retry_works`

Test the contract at the narrowest level that proves it without coupling the
test to incidental implementation.

Before declaring a durable change complete, walk the failure categories that
apply:

- concurrency and ordering;
- partial failure and retry;
- unbounded growth;
- restart and shutdown;
- serialization and migration.

Handle the relevant case, test it, or document why it is outside the current
contract. The walk is required; forcing every category into every change is
not.

## 7. Add only extension seams the task earned

Make the next confirmed variant easy to add without building machinery for
imagined variants. Present behavior and an explicitly named next step count
as pressure; "we may need this someday" does not.

An extension point is successful when a future contributor can locate where a
new variant belongs without reading the entire codebase. It is excessive when
it adds configuration, registries, policies, or alternate paths with no
current caller.

## 8. Make command outcomes honest

Command output is evidence and costs context. Structure exploratory commands
so an expected absence stays an ordinary result: use an explicit `if`, `test`,
or narrowly scoped `|| :` for optional paths and no-match searches. Do not
let an optional trailing `grep`, `ls`, or response parser mark an otherwise
useful probe as failed.

Never blanket-suppress a real test, build, migration, write, or patch failure.
Those non-zero exits are evidence. Fix the invocation or the owning problem,
then re-run the narrow check that proves the outcome.

Keep command output small in any repository: every later step re-reads it.
Prefer a tool's quiet or summary mode (`pytest -q`, `--silent`, a dot
reporter). Otherwise save the full output and read only its end and failures,
keeping the real exit status:
`cmd > "$TMPDIR/run.log" 2>&1; s=$?; tail -n 40 "$TMPDIR/run.log"; (exit $s)`,
then `grep` the log for the failing lines. Never print a whole log to find one
line.

## Pre-ship review

Scale the process to the change:

- **Small and local:** inspect the owning path, edit, run a focused test, then
  perform the review below.
- **Structural:** understand, design, review, implement, and review again.
- **High blast radius:** add an explicit plan plus failure, migration, and
  rollback analysis.

Re-read the change in three passes:

1. **Contract:** does the action still mean what its label promises, is its
   forbidden outcome protected, and did any existing guard change meaning?
2. **Design:** is the behavior owned at the right seam, with every wrapper,
   shape, special case, and extension point earning its complexity?
3. **Evidence:** do names and comments make the rationale discoverable, and
   are applicable failure modes covered without suppressing a real failure?

If the project's conventions welcome it, leave a one-line trace of this pass
in the commit, review description, or visible completion summary. Record
unresolved debt in the project's established durable location. Put it in production code only when a reader
needs that context to understand or change the code safely.
