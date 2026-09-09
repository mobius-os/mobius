"""AskUserQuestion lifecycle.

Both Claude and Codex SDK runners INSERT into the registry when the
agent calls AskUserQuestion. The POST /messages route resolves a
pending future by peeking with `get`, identity-reclaiming with
`claim_if`, then setting the result. Stop flows cancel the pending
future via `cancel`.

Timeout SLA — none. A question is a human pause point, so every provider
waits until the user answers or the owner explicitly stops the turn. A
pending question's future is resolved or cancelled by one of:
  (a) the user-answer POST → resolves with the answers dict, or
  (b) `stop_chat` / `stop_chat_for` → cancels the future.

If neither user answer nor stop fires, the
future may remain pending forever. This is intentional: the question card
blocks the user's chat UI,
so a silent timeout would silently drop their turn. Resolving with no
answer would be worse than blocking — the agent would interpret the empty
payload as a real choice and proceed with garbage state. If the process
restarts while a question is open, the in-memory future is gone, but the
durable transcript keeps the question block; the answer route records the
later answer and starts a hidden continuation so the collaboration can
resume.

`pending_questions.py` keeps the `PendingQuestion` dataclass alone so
the type can be shared without dragging this module's globals into
the runners. This file owns the registry + lifecycle on top of that
dataclass.

Design note — two ways to pause. `AskUserQuestion` above is an IN-TURN wait:
the turn's process stays alive, suspended on the future, and the wait is lost
on restart. A saved owner-input card (request_question / request_approval /
secure-input) is instead a DURABLE wait — the card and `pending_question_id`
persist, the turn ENDS so the process is released, and the answer route
resumes a fresh turn (restart-safe). Because the card's receipt returns to the
model immediately, the runner interrupts the live turn the moment such a card
commits, so nothing follows the card; see `ChatEventSink.publish_question` and
each runner's `finish_after_owner_card`.
"""

from __future__ import annotations

from app.pending_questions import PendingQuestion


# Module-level singleton registry. Runners receive this dict (or an
# alias of it) via the existing `pending_questions=` DI kwarg and
# write the PendingQuestion in directly under the chat_id key. Routes
# import `app.questions` and call `get` / `claim_if` / `cancel` / etc.
_pending: dict[str, PendingQuestion] = {}
_cancelled: dict[str, str | None] = {}


def accepts_saved_answer(
  chat,
  question_id: str | None,
) -> bool:
  """Whether an answer to `question_id` should be accepted for this chat.

  Primary signal is the durable `pending_question_id` marker, so an answer
  lands even when parallel tool/subagent output or a terminal error trails the
  card, and across a restart. Fallback: a *targeted* answer (a specific
  question_id) is still honored when the marker has cleared but that exact card
  is unanswered in the latest turn — answering the card after a Stop is a fresh
  continuation request, not a stale race. Position-independent; a later user
  turn (the decision was superseded) is not eligible.
  """
  open_id = chat.pending_question_id
  if open_id is not None:
    return question_id is None or question_id == open_id
  if not question_id:
    return False
  for msg in reversed(chat.messages or []):
    if msg.get("hidden"):
      continue
    if msg.get("role") != "assistant":
      return False
    return any(
      block.get("type") == "question"
      and block.get("question_id") == question_id
      and not block.get("answers")
      for block in (msg.get("blocks") or [])
    )
  return False


def saved_question(chat, question_id: str | None) -> dict | None:
  """Find one exact durable card, including an already acknowledged answer."""
  if not question_id:
    return None
  for message in reversed(chat.messages or []):
    for block in message.get("blocks") or []:
      if block.get("type") == "question" and block.get("question_id") == question_id:
        return block
  return None


def has_quiet_options(card: dict) -> bool:
  return any(
    option.get("on_answer") == "close"
    for question in card.get("questions", [])
    for option in question.get("options", [])
  )


class AnswerConflict(ValueError):
  """A saved answer needs an owner-visible correction, not an automatic retry."""


def closes_without_reply(card: dict, answers: dict, selections: dict | None) -> bool:
  """Resolve explicit selections against immutable card options, not prose.

  A mixed answer or free text keeps the normal continuation. Invalid supplied
  identities are conflicts rather than silently becoming a different action.
  """
  if card.get("response_mode") != "continuation" or not selections:
    return False
  specs = {question["id"]: question for question in card.get("questions", [])}
  if not set(selections).issubset(specs):
    raise AnswerConflict("The selected options do not belong to this question.")
  quiet = set(selections) == set(specs)
  for key, selected in selections.items():
    spec = specs[key]
    options = {option.get("id"): option for option in spec.get("options", [])}
    if (not selected or len(set(selected)) != len(selected)
        or any(identity not in options for identity in selected)):
      raise AnswerConflict("The selected option is no longer available.")
    if answers.get(spec["question"]) != ", ".join(options[identity]["label"] for identity in selected):
      raise AnswerConflict("Your answer does not match the selected options.")
    quiet = quiet and all(options[identity].get("on_answer") == "close" for identity in selected)
  if quiet and set(answers) != {spec["question"] for spec in specs.values()}:
    raise AnswerConflict("The answer does not match this question card.")
  return quiet


def open_continuation_question(chat, question_id: str | None) -> dict | None:
  """Find an exact open card whose answer belongs to a subsequent turn.

  The durable marker is authoritative even while its publishing turn is still
  finishing. Native provider questions retain their existing future contract.
  """
  if not question_id or chat.pending_question_id != question_id:
    return None
  for message in reversed(chat.messages or []):
    for block in message.get("blocks") or []:
      if (block.get("type") == "question"
          and block.get("question_id") == question_id
          and block.get("response_mode") == "continuation"
          and not block.get("answers")):
        return block
  return None


def continuation_question_owner_run_id(
  chat, question_id: str | None,
) -> str | None:
  """Return the run identity that durably authored an open continuation card."""
  if not question_id or chat.pending_question_id != question_id:
    return None
  return saved_question_owner_run_id(chat, question_id)


def saved_question_owner_run_id(chat, question_id: str | None) -> str | None:
  """Resolve the exact author even after its card has been stopped or closed."""
  if not question_id:
    return None
  for message in reversed(chat.messages or []):
    if not isinstance(message, dict):
      continue
    for block in message.get("blocks") or []:
      if (block.get("type") == "question"
          and block.get("question_id") == question_id
          and block.get("response_mode") == "continuation"
          and not block.get("answers")):
        owner = message.get("id")
        return owner if isinstance(owner, str) and owner else None
  return None


def is_secure_question(chat, question_id: str | None) -> bool:
  """Plaintext answer surfaces must never settle a sealed-input receipt."""
  target = question_id or chat.pending_question_id
  for message in reversed(chat.messages or []):
    for block in reversed(message.get("blocks") or []):
      if block.get("type") == "question" and (
        target is None or block.get("question_id") == target
      ):
        return bool(block.get("secure_input"))
  return False


def question_memory_diagnostics() -> dict[str, int]:
  """Return registry cardinalities without exposing answers or futures."""
  return {
    "pending_count": len(_pending),
    "cancelled_count": len(_cancelled),
  }


def register(chat_id: str, pending: PendingQuestion) -> None:
  """Inserts a pending question, replacing any existing entry.

  Mirrors today's `_pending_questions[chat_id] = pending` write from
  the runners. Exists as a named function so non-runner callers
  (tests, future routes) don't have to know the storage shape.
  """
  if not chat_id:
    return
  _cancelled.pop(chat_id, None)
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


def is_waiting(chat_id: str) -> bool:
  """Whether a question is still genuinely waiting for an owner answer.

  A resolved or cancelled future can remain in the registry briefly while the
  provider callback unwinds. That entry is useful to its owning runner, but it
  is no longer waiting for an owner answer.
  """
  pending = _pending.get(chat_id)
  return pending is not None and not pending.future.done()


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
    _cancelled.pop(chat_id, None)
    return True
  return False


def was_cancelled(chat_id: str, question_id: str | None = None) -> bool:
  """Whether Stop explicitly cancelled this chat's pending question.

  This is an in-memory tombstone, intentionally lost on process restart.
  A lost process should be recoverable from the durable transcript; an
  explicit Stop should not be rehydrated by a racing answer POST.
  """
  if chat_id not in _cancelled:
    return False
  cancelled_id = _cancelled[chat_id]
  return question_id is None or cancelled_id is None or cancelled_id == question_id


def cancel(chat_id: str) -> None:
  """Cancels and drops any live AskUserQuestion for the chat.

  Used by explicit Stop. Steering is refused while a question is waiting: the
  provider control channel cannot accept it until this same future resolves,
  and waiting for that acknowledgement while holding chat locks deadlocks the
  Stop escape. Idempotent on a missing entry. Pop-first ordering is
  functionally equivalent to get + cancel + pop (single-thread asyncio means
  no concurrent caller can slip between operations) but reads cleaner and
  removes the temptation to re-fetch by chat_id.
  """
  pending = _pending.pop(chat_id, None)
  if pending is None:
    return
  _cancelled[chat_id] = pending.question_id
  if not pending.future.done():
    pending.future.cancel()
