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
user message: ``StartTurn`` and ``AppendPending``.
"""

from sqlalchemy import update
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


def admit_scoped_first_turn(db: Session, chat: models.Chat) -> None:
  """Stamp this chat's own pending binding admitted, in the caller's open
  writer transaction.

  One conditional UPDATE, executed now rather than at flush (writer sessions
  do not autoflush): it takes the write lock with the accepted message, so a
  cancel either commits first and wins, or waits and finds the turn admitted.
  It never creates a binding, so a grouped chat sent outside ``/start`` stays
  an ordinary chat, and a sibling never stamps another chat's binding.
  """
  app_id = chat.created_by_app_id
  scope = chat_scope(chat)
  if app_id is None or scope is None:
    return
  db.execute(update(models.AppChatScopeStart).where(
    models.AppChatScopeStart.app_id == app_id,
    models.AppChatScopeStart.scope == scope,
    models.AppChatScopeStart.chat_id == chat.id,
    models.AppChatScopeStart.cancelled_at.is_(None),
    models.AppChatScopeStart.admitted_at.is_(None),
  ).values(admitted_at=now_naive_utc()).execution_options(
    synchronize_session=False,
  ))
