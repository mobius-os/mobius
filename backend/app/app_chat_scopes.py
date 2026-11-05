"""The durable start identity of app-scoped chats (``AppChatScopeStart``).

An app scope has two meanings. For ``POST /api/app-chats/start`` it is one
exact job: at most one first turn per app and scope, ever. For embedded chat
pickers it groups several chats. Only start and cancel create or fence this
record; grouping neither reads nor claims it.

``/start`` commits the binding before it sends the first turn, so admission
can be recorded in the same commit that accepts that turn. Otherwise a crash
after acceptance, followed by the owner's Stop or queue removal, would leave a
replay no evidence that the job ran. The chat writer therefore calls
``admit_scoped_first_turn`` from exactly the two commands that accept a new
user message: ``StartTurn`` and ``AppendPending``. The command ``/start``
issues carries its exact scope identity, so a cancel that commits first makes
the writer refuse it even after the request itself has timed out.
"""

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app import models
from app.chat_visibility import coerce_agent_settings
from app.timeutil import now_naive_utc

# The scope column's width, which the app-chat routes also validate.
SCOPE_MAX = 160


def chat_scope(chat: models.Chat) -> str | None:
  """The app-defined scope a chat was created under, if any."""
  value = coerce_agent_settings(chat.agent_settings_json).get("chat_scope")
  return value.strip() if isinstance(value, str) and value.strip() else None


class ScopedStartCancelled(Exception):
  """The app cancelled this scope before the writer accepted ``/start``'s
  first turn; the writer rolled the whole command back."""


def admit_scoped_first_turn(
  db: Session, chat: models.Chat, scoped_start: tuple[int, str] | None = None,
) -> None:
  """Stamp this chat's own binding admitted, in the caller's open writer
  transaction.

  One conditional UPDATE, executed now rather than at flush (writer sessions
  do not autoflush): it takes the write lock with the accepted message, so a
  cancel either commits first and wins, or waits and finds the turn admitted.
  It never creates a binding, so a grouped chat sent outside ``/start`` stays
  an ordinary chat, and a sibling never stamps another chat's binding.

  ``scoped_start`` is the exact ``(app_id, scope)`` that ``/start`` put on its
  writer command. That acceptance is valid only while the binding is live, so
  a miss raises ``ScopedStartCancelled`` and the command commits nothing: the
  ``/start`` request may have timed out and released its lock before the
  writer ran this command. An ordinary send carries no identity and is never
  refused; it only records admission when the binding is still live.
  """
  app_id, scope = scoped_start or (chat.created_by_app_id, chat_scope(chat))
  if app_id is None or scope is None:
    return
  binding = models.AppChatScopeStart
  admitted = db.execute(update(binding).where(
    binding.app_id == app_id,
    binding.scope == scope,
    binding.chat_id == chat.id,
    binding.cancelled_at.is_(None),
  ).values(
    admitted_at=func.coalesce(binding.admitted_at, now_naive_utc()),
  ).execution_options(synchronize_session=False))
  if scoped_start is not None and admitted.rowcount != 1:
    raise ScopedStartCancelled(f"app scope start cancelled for chat {chat.id}")
