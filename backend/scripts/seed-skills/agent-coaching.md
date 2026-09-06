---
name: agent-coaching
description: Coach a previous Möbius chat agent, app/background agent, or pipeline through a neutral evidence-based review. Use when the partner asks to coach, interview, debrief, run a 1-on-1 with, or learn from an agent; when they ask why an earlier agent made a decision; or when Reflection coaches recent agents for self-improvement.
---

# Agent coaching

Agent coaching is a neutral learning conversation, not an interrogation or a
performance verdict. Recover the agent's own context when possible, recognise
what it did well, invite honest reflection about what could improve, and turn
the resulting lessons into the smallest durable improvement at the surface
that owns the behaviour.

Use the same method whether the partner requests the coaching live or
Reflection selects a recent agent during its unattended self-improvement run.

## 1. Ground the coaching in evidence

Identify the exact chat or app run first. A title is not a stable identity:
resolve it through the chat record, the recent-chat summary location supplied
to this session, or the owning app's run record. If two targets plausibly match,
ask the partner which one.

Read the summary or relevant transcript section before writing the coaching
prompt. Gather the smallest useful evidence set: the outcome, the decision or
friction worth discussing, and the owning source that can verify consequential
claims. For a Memory or Reflection review, the dedicated Reflection evidence
helper can consolidate that subsystem's run evidence:

```bash
python3 /data/platform/backend/scripts/reflection-evidence.py \
  [--hours 72] [--limit 5] [--traces 12] [--memory-writer-packet] [CHAT_ID]
```

Do not begin with an accusation. State the concrete outcome neutrally and keep
observation, testimony, and inference separate.

Never fork a deleted chat. Deletion leaves read-only evidence during recovery,
not permission to revive its provider session. If the exact provider session
cannot be forked, coaching is unavailable for that chat.

## 2. Start with useful, specific feedback

Open with genuine evidence-cited feedback about what the agent did well. This
is not praise padding: naming a sound decision, honest uncertainty, efficient
move, or well-finished outcome makes the review fair and gives the agent a
concrete strength to preserve.

Then name one or two specific opportunities to improve, also tied to evidence.
Use neutral language such as "I noticed…", "What led you to…?", and "What would
have made this easier to catch?" Avoid prosecutorial framing, leading questions,
or telling the agent the lesson before it has reflected. The aim is candour,
not agreement or defensiveness.

## 3. Fork the historical agent's exact session

For a chat agent, use the platform helper with structured provenance:

```bash
timeout 150 /data/platform/backend/scripts/fork-chat.sh --json \
  <chat_id> "<focused coaching prompt>"
```

Give the outer tool call more time than the inner limit (normally 170 seconds;
up to 300/280 seconds for one genuinely giant, high-value chat). The original
chat and transcript are never modified.

Success has exactly one provenance: `method: session_fork` and
`exact_session_fork: true`. The named Claude or Codex provider session was
branched into a new throwaway session, and the coaching prompt ran there.
Require a non-empty `forked_session_id` different from `source_session_id`.

For an app or background run whose owning evidence supplies a provider, session
id, and working directory, use:

```bash
timeout 150 /data/platform/backend/scripts/fork-session.sh \
  <claude|codex> <session_id> <cwd> "<focused coaching prompt>"
```

Do not scan provider credential or session stores to discover ids; use only ids
surfaced by the owning run record.

Treat empty, very short, malformed, timed-out, auth/quota, or provider-error
output as failed coaching. Do not reseed another agent from a transcript, offer
an evidence-only substitute, or claim coaching occurred. Say that exact-session
coaching was unavailable and stop that coaching branch.

## 4. Ask questions that invite learning

Specialise the prompt around what the agent actually did. Use this sequence as
a frame, not a questionnaire that must be repeated word-for-word:

1. **Reflect on the outcome.** What were you trying to achieve, and what shaped
   the decision you made?
2. **Preserve strengths.** What do you think went well, and what evidence makes
   that worth repeating?
3. **Find the improvement.** Looking back, what could you have noticed,
   reconsidered, or done differently? What made that difficult at the time?
4. **Extract the lesson.** What is the most general lesson another agent should
   carry into similar work?
5. **Translate it.** Would that lesson be best encoded in a skill, governing
   prompt, tool or script, platform primitive, app workflow, Memory, or nowhere
   durable? What is the smallest change that would have helped without adding
   a workaround or a second mechanism?

Ask the agent to distinguish what it remembers or observes from what it is
inferring now, and to cite checkable evidence for consequential claims. During
Reflection, include one explicit self-improvement question: **what should
Reflection itself change about how it selects, coaches, verifies, or acts on
agents next time?**

Do not ask questions already answered well in an earlier coaching record. Go
deeper on new evidence instead of repeating a ritual.

## 5. Verify, synthesize, and improve the owner

Coaching is testimony, not ground truth. Verify consequential claims against
the transcript, filesystem, Git, tests, logs, app records, or current
documentation that owns the fact. When testimony conflicts with those sources,
trust the owning source and state the mismatch.

Synthesize three things:

1. the strength worth preserving;
2. the specific improvement and general lesson; and
3. the proposed durable change, if one is earned.

Inspect every plausible owning surface—skills, governing prompt, scripts/tools,
platform primitive, app workflow, and Memory—but change only the one that owns
the cause. If several agents expose the same handoff or primitive problem,
name the cross-cutting lesson after keeping each agent's feedback distinct.

Co-design the fix from the agent's suggestion, then apply the smallest durable
version. Follow the normal approval rules: a partner-initiated coaching request
does not by itself authorise behaviour-changing edits, risky operations, or a
restart. An unattended Reflection run makes only conservative reversible
changes and leaves other proposals in its brief.

In the closeout, say that coaching used an exact session fork, including the
provider. If the fork failed, say coaching did not complete; do not replace it
with a differently sourced review.
