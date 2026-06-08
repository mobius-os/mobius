"""Small datetime helpers shared across soft-delete write/compare sites."""

from datetime import datetime, timedelta, UTC

# Single source of truth for the soft-delete recovery window. Both App
# (routes/apps.py) and Chat (routes/chats.py) tombstones are hard-purged this
# long after deletion, so the two recovery windows can't silently drift apart.
# See feature 110 (app uninstall) + the chat soft-delete it mirrors.
SOFT_DELETE_TTL = timedelta(days=7)


def now_naive_utc() -> datetime:
  """Returns the current UTC time as a NAIVE datetime.

  The soft-delete columns (`App.deleted_at`, `Chat.deleted_at`) are plain
  `Column(DateTime)`, so SQLite stores and returns naive values. Writing the
  current time as `datetime.now(UTC).replace(tzinfo=None)` keeps the in-process
  value consistent with what comes back from the DB and dodges the py3.11+
  aware/naive `TypeError` when a freshly-written value is subtracted before a
  refetch (the TTL-purge compare). This one helper replaces that boilerplate at
  every `deleted_at` write + compare site so App and Chat agree on the format.
  """
  return datetime.now(UTC).replace(tzinfo=None)
