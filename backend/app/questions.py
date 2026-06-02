"""AskUserQuestion lifecycle.

Both Claude and Codex SDK runners INSERT into the registry when the
agent calls AskUserQuestion. The POST /messages route resolves a
pending future via `claim` + setting the result. Stop flows cancel
the pending future via `cancel`.

Timeout SLA — there is none. A pending question's future is resolved
only by one of:
  (a) the user-answer POST → resolves with the answers dict, or
  (b) `stop_chat` / `stop_chat_for` → cancels the future.

If neither fires (container crash, lost client, stuck SDK), the
future stays alive until the process restarts. This is intentional:
the question card blocks the user's chat UI, so a silent timeout
would silently drop their turn. Resolving with no answer would be
worse than blocking — the agent would interpret the empty payload as
a real choice and proceed with garbage state. Investigate any sign
of a leaked future in chat.log, don't paper over it with a deadline.

`pending_questions.py` keeps the `PendingQuestion` dataclass alone so
the type can be shared without dragging this module's globals into
the runners. This file owns the registry + lifecycle on top of that
dataclass.
"""

from __future__ import annotations

from app.pending_questions import PendingQuestion


# Module-level singleton registry. Runners receive this dict (or an
# alias of it) via the existing `pending_questions=` DI kwarg and
# write the PendingQuestion in directly under the chat_id key. Routes
# import `app.questions` and call `claim` / `cancel` / etc.
_pending: dict[str, PendingQuestion] = {}


def register(chat_id: str, pending: PendingQuestion) -> None:
  """Inserts a pending question, replacing any existing entry.

  Mirrors today's `_pending_questions[chat_id] = pending` write from
  the runners. Exists as a named function so non-runner callers
  (tests, future routes) don't have to know the storage shape.
  """
  if not chat_id:
    return
  _pending[chat_id] = pending


def deliver_answer(chat_id: str, answers: dict) -> bool:
  """Resolves a pending AskUserQuestion with the partner's answers.

  Returns True if a pending question was waiting and was resolved,
  False if no pending question exists (caller should fall through to
  the normal queue path). Idempotent — if the future is already done
  (race with stop), returns True without re-resolving.
  """
  pending = _pending.get(chat_id)
  if pending is None:
    return False
  if not pending.future.done():
    pending.future.set_result(answers)
  return True


def get(chat_id: str) -> "PendingQuestion | None":
  """Accessor for the pending-question registry.

  Tests + debug routes use this; the run loop owns set/clear directly.
  """
  return _pending.get(chat_id)


def claim(chat_id: str) -> "PendingQuestion | None":
  """Atomically removes and returns the pending question for a chat.

  POST /messages uses this to short-circuit the queue path on
  answer-delivery — once claimed, no other caller can resolve the
  same future.
  """
  return _pending.pop(chat_id, None)


def claim_if(chat_id: str, expected: "PendingQuestion") -> bool:
  """Pop the pending question ONLY if it is still `expected` (by identity).

  The stop-races-answer guard: the answer route PEEKS the pending entry,
  submits AnswerQuestion to the actor, and AWAITS its ack — during that
  await a concurrent Stop can `cancel()` (pop + cancel the future) the
  same chat's question. After the ack, the route calls this to re-claim
  the entry by identity before resolving its future. Returns True (and
  removes it) when the registry still holds exactly `expected`; False
  when it was removed or replaced (Stop cancelled it, or a newer question
  superseded it) — in which case the caller must NOT resolve the future
  (it is already cancelled / belongs to a different question) and returns
  410. Single-thread asyncio makes the check-and-pop atomic (no await
  between get + pop).
  """
  current = _pending.get(chat_id)
  if current is expected:
    _pending.pop(chat_id, None)
    return True
  return False


def cancel(chat_id: str) -> None:
  """Cancels and drops any live AskUserQuestion for the chat.

  Idempotent on a missing entry. Pop-first ordering is functionally
  equivalent to today's get + cancel + pop (single-thread asyncio
  means no concurrent caller can slip between operations) but reads
  cleaner and removes the temptation to re-fetch by chat_id.
  """
  pending = _pending.pop(chat_id, None)
  if pending is not None and not pending.future.done():
    pending.future.cancel()
