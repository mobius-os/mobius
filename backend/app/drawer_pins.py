"""One process-wide commit boundary for the drawer's shared pinned set."""

from collections.abc import Iterator
from contextlib import contextmanager
import threading


_WRITE_LOCK = threading.Lock()


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
