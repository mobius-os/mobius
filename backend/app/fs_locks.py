"""In-process async locks serializing storage-tree and source-tree mutations
against app uninstall.

Möbius runs a SINGLE uvicorn worker (entrypoint.sh execs `uvicorn ...` with no
`--workers`), so an in-process ``asyncio.Lock`` is a complete serialization
primitive — there is no second worker to coordinate with, and FastAPI runs
these async handlers on the one event loop. This closes two TOCTOU races a
multi-tab owner could otherwise hit (Codex review round-6 #3, #4):

  - a per-app storage write that pauses to read its body, then recreates
    ``/data/apps/<id>`` AFTER an interleaved uninstall removed it, and
  - a create/patch that assigns a ``source_dir`` in the window between
    uninstall's "is this dir shared?" check and its ``rmtree``.

Both locks follow the per-chat lock pattern in ``chat_queue``: a
``WeakValueDictionary`` so an idle lock garbage-collects itself (the dict can't
grow unbounded), and the get-or-create is atomic from the event loop's point of
view (no ``await`` between the lookup and the insert).
"""

import asyncio
from weakref import WeakValueDictionary

_app_locks: "WeakValueDictionary[int, asyncio.Lock]" = WeakValueDictionary()
_source_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()


def app_storage_lock(app_id: int) -> asyncio.Lock:
  """Serializes per-app storage writes with that app's uninstall cleanup.

  Held by ``write_app_file`` around its existence-recheck + atomic write, and
  by ``delete_app`` around the ``/data/apps/<id>`` storage-tree removal, so a
  write can never recreate the tree after uninstall deleted it.
  """
  lock = _app_locks.get(app_id)
  if lock is None:
    lock = asyncio.Lock()
    _app_locks[app_id] = lock
  return lock


def source_dir_lock(resolved: str) -> asyncio.Lock:
  """Serializes source_dir assignment with uninstall's source-tree cleanup.

  Held by ``create_app``/``patch_app`` around assigning a source_dir + commit,
  and by ``delete_app`` around its shared-dir dedup check + ``rmtree`` for the
  same resolved directory, so a concurrent create can't claim a directory in
  the window between the dedup check and the delete.
  """
  lock = _source_locks.get(resolved)
  if lock is None:
    lock = asyncio.Lock()
    _source_locks[resolved] = lock
  return lock
