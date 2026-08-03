# Manager session — the on-demand 1-on-1

Run this when the partner asks for a "manager session", a "1-on-1", or a
"performance review" of one of their agents (Memory, Reflection, a cron/app
agent, a chat agent) or of a whole pipeline. It is a structured coaching review:
open with genuine, evidence-cited **praise**, name the specific things that
could be **improved**, then co-design **changes that target future improvement
across every relevant surface** — the agent's skills, its governing/system
prompt, *and* the scripts and tools it runs. You interview the agent about its
own behaviour, verify what it tells you, and apply the smallest durable fix once
the partner approves anything behaviour-changing. `Read` this before starting one
so the ritual is consistent every time and the partner never has to re-explain
it.

This is a *manager's* review, not a bug hunt: the deliverable is a balanced
1-on-1 that makes the agent measurably better next week, not a defect list. It
scales from one agent to a whole pipeline (one 1-on-1 per role).

This skill is agent-editable (it lives under `/data/shared/skills/`) — sharpen
it when a session teaches you a better move, but keep it a clean, de-dated
ruleset. Incidents belong in Memory, not here.

---

## The shape of one session

Every session, for every agent reviewed, walks the same five beats in order:

1. **Gather evidence in one call** — start from the companion script, not a
   dozen sequential tool calls.
2. **Coach** — evidence-cited praise first, then specific, fair improvements.
   A balanced 1-on-1, never just what broke.
3. **Interview the agent** — fork its real session and ask it to introspect.
   Capture answers verbatim. Verify before you trust.
4. **Design the fix across all surfaces** — skills, system/governing prompt,
   *and* the scripts/tools it runs. Consolidating tool output is a first-class
   lever, not an afterthought.
5. **Co-design and apply** — agree the smallest durable fix with the agent, then
   apply it only after the partner approves any behaviour-changing edit.

Do not force a beat that has no signal — a healthy agent earns a short session —
but do them in this order, because the interview reshapes the coaching and the
coaching reshapes the fix.

---

## 1. Start from one call — the evidence bundle

Before opening any transcript, run the companion script. It exists precisely so
a session starts from **one consolidated report** instead of six sequential
tools (read Memory's run-status, tail the update log, cross-check read-traces
against the chat DB, tail Reflection's metrics, list Reflection run artifacts, curl
the skills API). One call, one readable bundle:

```bash
python3 /data/shared/skills/manager-session-evidence.py [--hours 72] [--limit 5] [--traces 12] [CHAT_ID]
```

It prints, for the memory + reflection agents: run-status and the last few
consolidation outcomes (counts, deletes, problems, followups); recent reflection
exit codes / durations / brief-written; the recent read-trace count with the
last N traces mapped to their chat titles; which artifact files each Reflection
run left; and platform-wide skill-load counts. Pass a `CHAT_ID` to profile one
chat (title, provider, message count, whether Memory recalled into it, and the
exact fork command to use in beat 3). It is read-only and defensive: missing
pieces are skipped, never fatal.

For a Memory writer review, add `--memory-writer-packet`. The same call appends
one writer outcome, a terminal receipt only when its non-empty `run_id` matches
exactly, the applied diff limited to changed or deleted memory paths, that
run's recall verdicts, and the current Memory governing skill plus writer prompt
builder. It includes no raw chat bodies and supports a stateless evidence
review, not an interview.

Read the bundle first and let it *point* you — a run that failed, a followup that
keeps recurring, a skill an agent leaned on hard, a chat where recall served
nothing. That is where the session's value is. Only reach for raw logs to
resolve a specific uncertainty the bundle raised.

**The script itself is in scope for improvement.** If a session needed a number
the bundle did not carry, the right fix is often to teach the script to emit it
(beat 4), so the *next* session starts even closer to the answer.

---

## 2. Coach — praise with proof, then fair improvement

A manager session is a balanced 1-on-1. Open with **genuine praise, cited to
evidence** — not "good job", but "your last consolidation promoted nine durable
facts with zero bad deletes and flagged two honest followups" (from the update
log), or "five clean nightly runs, every one shipped a brief" (from the metrics).
If
you cannot cite it, it is not praise, it is flattery — dig until you find the
real thing the agent did well, because there almost always is one and naming it
is what makes the improvement land.

Then name the **specific things that could be improved**, each tied to the same
kind of evidence: a recurring followup that never closes, a skill the agent
complained about, a read that served stale notes, a run that timed out. Be
concrete and fair. One well-evidenced improvement the agent can act on beats a
long list it will tune out. Keep the ratio honest — this is coaching, so the
praise is real and the critique is specific, and neither is padding.

---

## 3. Interview the agent — fork, ask, verify

The agent that did the work holds context the artifacts don't: *why* it made a
call, *what in its instructions* pushed it there, *what would have helped*. You
recover that by **forking its real session and asking it** — never by editing
the original.

**Fork mechanics.** The bundle's focus block prints the right command; the
general forms are:

```bash
# a chat agent (chat id from the DB / the bundle):
/data/apps/reflection/fork-chat.sh <chat_id> "<interview question>"

# an app or cron/background session (session id + its cwd):
/data/apps/reflection/fork-session.sh <session_id> <cwd> "<interview question>"
```

**Time-box every fork.** Pass the Bash tool `timeout: 300000` and set the inner
shell just below it (`timeout 280 ...`) so the inner limit wins and you capture
partial output instead of an empty file. **Large codex forks are slow to first
token** — even a modest codex session can blow past the default 120s wall, so
budget the higher timeout for any codex fork, not just big ones. After the fork,
check `[ -s <out> ]`: if it came back **empty, stateless, or junk** (an aged-off
session resumes with no output; a provider out of credits errors), fall back to
the raw record — read the chat's `messages` JSON from `/data/db/ultimate.db` (or
the session jsonl) and synthesise from the last several messages. Forks are a
convenience; the transcript is always there.

**What to ask** (specialise per agent — read what it actually did first):

1. **Why did this happen?** Walk me through the call you made and why.
2. **What in your instructions led here?** Which skill, prompt line, or tool
   output shaped the decision? (This is the lever you will pull in beat 4.)
3. **What would have helped?** A missing rule, a sharper contract, a tool that
   returned the answer in one call instead of five.

**Capture answers VERBATIM** into the session's working notes — the agent's own
words are the primary signal, and paraphrase loses the tell. If you fell back to
empirical analysis because the fork was empty or the agent is stateless, **say
so plainly** ("fork returned nothing; the following is reconstructed from the
transcript / from instantiating its prompt against the evidence"), and mark it as
your inference, not its testimony.

**Testimony is not ground truth — verify before acting.** A forked agent missing
recent state will confidently invent a plausible cause or claim a fix that never
landed. Make it cheap to check: whenever the agent cites a change, `grep` the
cited token in the file it named, `stat` the mtime, or confirm the commit. If the
claim isn't on disk, the interview confabulated — trust the filesystem and put
the issue back on the table. Treat mismatches as the expected case, not the rare
one.

---

## 4. Design the fix across ALL surfaces

The interview's answer to "what in your instructions led here?" tells you which
surface to change. A manager session's distinguishing move is that it looks at
**every** surface, not just the obvious one:

- **Skills** — the reusable procedure the agent reads (`/data/shared/skills/`).
  A missing gotcha, a stale contract, a rule that misled. Smallest surgical edit.
- **System / governing prompt** — the agent's own instructions (its runner
  prompt, its app-owned skill, its cron scaffold). When the *prompt* pushed the
  wrong call, fix the prompt — that is often the true root cause a skill edit
  only papers over.
- **Scripts and tools — a first-class lens.** If the agent had to run many tools
  sequentially to assemble something it needed, the durable fix is to make the
  script or tool **return the relevant information in ONE call**. Batch and
  consolidate output so the agent spends its turns on judgment, not plumbing.
  The evidence script in beat 1 is the worked example of this principle; apply
  the same lens to whatever the reviewed agent runs. Weak or missing analytics
  is itself a finding — an agent can't improve on a signal it never sees.

Pick the surface that owns the cause. Do not patch a script-shaped problem with a
skill line, or a prompt-shaped problem with a symptom check.

---

## 5. Co-design and apply

**Co-design the fix WITH the agent.** You already asked "what would have helped?"
— use its answer as the starting proposal, then converge on the **smallest
durable, future-proof change**: no symptom patch, no timer/retry/early-return to
dodge the cause, no speculative abstraction. The agent that hit the wall usually
knows the shape of the doorway; your job is to keep the fix minimal and general.

**Apply on the right authority.** A pure skill/script/analytics improvement that
changes no behaviour you can apply directly and commit. **Anything that changes
the agent's behaviour — a governing-prompt edit, a new default, a
behaviour-altering script change — waits for the partner's approval** before it
lands. Present it as a crisp proposal (what changes, why, the one-line diff),
apply on the nod, and verify the change is actually on disk.

---

## Scaling to a pipeline

A pipeline gets **one 1-on-1 per role**, not one blurred review of everything.
Run the five beats for each agent in turn (e.g. Memory, then Reflection, then a
cron agent), keeping each agent's praise/critique/fix separate — a fix for one
role rarely transfers cleanly to another, and merging them buries the specific
signal. Then, at the end, note the **cross-cutting** findings: a skill two agents
both tripped on, a tool every role had to call five times, a handoff between
roles that dropped context. Those shared fixes are the highest-leverage output of
a pipeline review.

For several agents, or for parallel sessions, this ritual **runs cleanly via
subagents or a workflow** — spawn one session per role so the interviews and
evidence-gathering happen in parallel, then collect each role's coaching + fix
proposal and consolidate the cross-cutting findings for the partner. The beats
and the one-call evidence bundle are identical whether a session runs inline or
as a spawned agent.

---

## Relationship to Reflection

This ritual is **adjacent to but distinct from** `reflection.md`'s phase-1
interviews. Reflection interviews run **unattended, overnight, on the strongest
signal**, and their output is aimed at **skills and the morning brief**. A
manager session is **on-demand, partner-initiated, and coaching-shaped**: it
produces balanced praise-and-improvement feedback and deliberately widens the fix
surface to include the **governing prompt and the scripts/tools** the agent runs,
not just its skills. The fork mechanics and the verify-before-you-trust rule are
shared; the framing, the cadence, and the deliverable are not. When both could
apply, let Reflection own the nightly sweep and reach for a manager session when
the partner wants to sit down with a specific agent and make it better.
