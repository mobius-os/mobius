"""One process-wide commit boundary for the drawer's shared pinned set."""

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
import threading


_WRITE_LOCK = threading.Lock()
_LATEST_INTENT_VERSIONS: OrderedDict[str, int] = OrderedDict()
_MAX_INTENT_CLIENTS = 128


@contextmanager
def serialized_write() -> Iterator[None]:
  """Keep pinned membership and rank commits linearizable.

  The drawer presents chats, apps, and projects as one ordered collection even
  though their membership and ranks live across three tables and lifecycle
  routes. The production server has one worker process, so this narrow critical
  section prevents one route from changing the identity set between another
  route's validation and commit without holding a lock across awaited work.
  """
  with _WRITE_LOCK:
    yield


def intent_is_superseded(client: str | None, version: int | None) -> bool:
  """Return whether a newer drawer intent already committed.

  Call only while holding ``serialized_write``. Each owner UI keeps one stable
  client id and monotonic counter, so a request that outlives its deadline
  cannot land after the newer action that unblocked that client's queue. The
  process-local witness is sufficient: an in-flight request cannot survive a
  server restart, and production deliberately runs one worker.
  """
  if client is None or version is None:
    return False
  # Equality is an already-handled request, not permission to assume an
  # unverified payload is identical. The caller returns current canonical
  # state, which also makes a lost-response retry idempotent.
  return version <= _LATEST_INTENT_VERSIONS.get(client, 0)


def record_committed_intent(client: str | None, version: int | None) -> None:
  """Advance the owner-intent witness after its database commit succeeds."""
  if client is None or version is None:
    return
  _LATEST_INTENT_VERSIONS[client] = max(
    _LATEST_INTENT_VERSIONS.get(client, 0), version,
  )
  _LATEST_INTENT_VERSIONS.move_to_end(client)
  while len(_LATEST_INTENT_VERSIONS) > _MAX_INTENT_CLIENTS:
    _LATEST_INTENT_VERSIONS.popitem(last=False)
