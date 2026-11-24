"""Which chats are rebuilding their context right now, for every viewer.

Manual compaction summarizes the chat in a disposable provider turn that can
take minutes while holding the chat's transition lock.
Sends wait behind that lock, so without a shared signal a reloaded tab, another
pane, or another device sees an idle chat whose new message silently sits in
flight. This process-local registry is the authority for that window: the
request that does the work marks it, chat reads report it, and the system
stream announces each edge so mounted views update without polling.

It is deliberately in-memory: the work is bound to the request's process, so a
restart ends both the work and the state together.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Literal

from app.broadcast import get_system_broadcast

CompactionKind = Literal["compact"]

_active: dict[str, CompactionKind] = {}
_guard = threading.Lock()


def compaction_kind(chat_id: str) -> CompactionKind | None:
  """Return the in-flight context rebuild for ``chat_id``, if any."""
  with _guard:
    return _active.get(chat_id)


def _publish(chat_id: str, kind: CompactionKind | None) -> None:
  get_system_broadcast().publish({
    "type": "chat_compaction_changed",
    "chatId": chat_id,
    "compacting": kind,
  })


@contextmanager
def compacting(chat_id: str, kind: CompactionKind) -> Iterator[None]:
  """Mark ``chat_id`` as rebuilding its context for the duration of the block.

  Callers hold the chat's transition lock, so at most one rebuild per chat is
  ever active and a plain set/remove is enough.
  """
  with _guard:
    _active[chat_id] = kind
  _publish(chat_id, kind)
  try:
    yield
  finally:
    with _guard:
      _active.pop(chat_id, None)
    _publish(chat_id, None)
