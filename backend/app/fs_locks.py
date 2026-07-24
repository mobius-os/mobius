"""In-process async locks serializing storage-tree and source-tree mutations
against app uninstall.

Möbius runs a SINGLE uvicorn worker (entrypoint.sh execs `uvicorn ...` with no
`--workers`), so an in-process ``asyncio.Lock`` is a complete serialization
primitive — there is no second worker to coordinate with, and FastAPI runs
these async handlers on the one event loop. This closes the TOCTOU races a
multi-tab owner could otherwise hit between an uninstall and a concurrent
storage write / source assignment / install (Codex review round-6 #3, #4 and
round-7 #1, #2):

  - a per-app storage write that pauses to read its body, then recreates
    ``/data/apps/<id>`` AFTER an interleaved uninstall removed it (or a write
    against a freed-then-reused id whose tree the old uninstall is removing),
  - a create/patch that assigns a ``source_dir`` in the window between
    uninstall's "is this dir shared?" check and its ``rmtree``, and
  - an install that materializes a source tree / storage seeds / cron entry an
    interleaved uninstall is tearing down.

A THIRD lock — the singleton install/uninstall lifecycle lock — serializes
whole installs against whole uninstalls, because an install materializes the
same three trees (source dir, storage seeds, cron) an uninstall removes, and
threading the two keyed locks through the 340-line installer in a deadlock-safe
order is far more error-prone than simply never letting the two lifecycle
operations overlap. Installs/uninstalls are infrequent owner actions, so one
global lock costs nothing in practice.

The keyed locks follow the per-chat lock pattern in ``chat_queue``: a
``WeakValueDictionary`` so an idle lock garbage-collects itself (the dict can't
grow unbounded), and the get-or-create is atomic from the event loop's point of
view (no ``await`` between the lookup and the insert).

LOCK ORDERING — every multi-lock holder acquires in this order, and nobody
acquires in reverse, so there is no cycle:

    install_uninstall_lock  ->  app_storage_lock(id)  ->  source_dir_lock(dir)

``shared_skills_lock`` is always innermost. Install sync takes lifecycle then
shared; uninstall/recover release any source-dir lock before taking shared.
No shared-skills holder ever acquires a lifecycle, app, or source lock.

Multi-lock holders, all acquiring left-to-right:

  - ``delete_app`` holds all three.
  - ``recover_app`` holds lifecycle -> app while it refreshes a stale bundle,
    then may take source and shared-skills locks further inside that span.
  - ``update_app`` (PATCH) and explicit app source apply hold
    lifecycle -> app -> source (PATCH takes the source lock only when the
    source_dir actually changes). Both recompile a bundle, so they take the
    app lock to serialize against each other and the lifecycle lock to block
    a concurrent uninstall + SQLite id reuse.

Single-lock holders: ``write_app_file`` / ``delete_app_file`` take only the
app lock; ``create_app`` takes only the source lock; the install endpoint
takes only the lifecycle lock.
"""

import asyncio
from pathlib import Path
from weakref import WeakValueDictionary

_app_locks: "WeakValueDictionary[int, asyncio.Lock]" = WeakValueDictionary()
_source_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()
_lifecycle_lock = asyncio.Lock()
_shared_skills_lock = asyncio.Lock()


def install_uninstall_lock() -> asyncio.Lock:
  """The singleton lock serializing whole installs against whole uninstalls.

  Held by the install endpoint (around ``install_from_manifest``) and by
  ``delete_app`` (as its OUTERMOST lock). Without it a concurrent install and
  uninstall race on the same /data/apps trees: uninstall can delete a source
  file the install just wrote, an install's post-commit cron registration can
  re-create a tree uninstall removed, and an install reusing a freed SQLite id
  can seed /data/apps/<id> that uninstall is mid-cleanup of (Codex review
  round-7 #2). Startup bootstrap installs skip it — nothing is serving yet, so
  no uninstall can run concurrently.
  """
  return _lifecycle_lock


def shared_skills_lock() -> asyncio.Lock:
  """The singleton lock serializing /data/shared/skills materialization.

  Held by the installer's post-commit skill-sync phase around the whole
  read-sidecar -> hash -> git-snapshot -> write -> record sequence, so two
  concurrent installs can't interleave between reading a skill file's hash
  and overwriting it (one would clobber the other's snapshot decision, or
  lose a sidecar record to a stale read-modify-write). One lock for the
  whole skills dir rather than per-file: every sync rewrites the single
  ownership sidecar anyway, and lifecycle changes are infrequent owner
  actions, so the coarse lock costs nothing. Uninstall/recover use the same
  lock to move app-owned files into/out of the inactive archive.
  """
  return _shared_skills_lock


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


def source_dir_lock(source_dir: str) -> asyncio.Lock:
  """Serializes source_dir assignment with uninstall's source-tree cleanup.

  Held by ``create_app``/``patch_app``/installer around assigning a source_dir
  + commit, and by ``delete_app`` around its shared-dir dedup check + ``rmtree``
  for the same directory, so a concurrent create can't claim a directory in the
  window between the dedup check and the delete.

  The key is CANONICALIZED here (``Path(...).resolve()``) so callers that pass a
  derived/unresolved path and callers that pass an already-resolved one map to
  the SAME lock — otherwise a symlinked or relative DATA_DIR would split them
  into two locks and silently lose serialization.
  """
  key = str(Path(source_dir).resolve())
  lock = _source_locks.get(key)
  if lock is None:
    lock = asyncio.Lock()
    _source_locks[key] = lock
  return lock
