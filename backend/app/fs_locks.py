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
  - a first explicit apply that claims a ``source_dir`` in the window between
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

    cwd_reservation(cwd)  ->  install_uninstall_lock
                          ->  app_storage_lock(id)  ->  source_dir_lock(dir)

``reserve_cwd_for_mutation`` is OUTERMOST and never acquired by a holder of
any lock to its right. It is taken by an agent turn around one tool call, and
that tool call can legitimately reach the install/uninstall path; nothing on
the lifecycle side runs an agent tool call, so the reverse edge does not
exist. It is also the only entry here that self-expires rather than relying
on its holder to release it — see its docstring.

``shared_skills_lock`` is always innermost. Install sync takes lifecycle then
shared; uninstall/recover release any source-dir lock before taking shared.
No shared-skills holder ever acquires a lifecycle, app, or source lock.

Multi-lock holders, all acquiring left-to-right:

  - ``delete_app`` holds all three.
  - ``recover_app`` holds lifecycle -> app while it refreshes a stale bundle,
    then may take source and shared-skills locks further inside that span.
  - explicit app source apply holds lifecycle -> app -> source for an existing
    app; first apply holds lifecycle -> source until the new row commits.

Single-lock holders: ``write_app_file`` / ``delete_app_file`` take only the
app lock; the install endpoint takes only the lifecycle lock.
"""

import asyncio
import logging
from pathlib import Path
from weakref import WeakValueDictionary

_app_locks: "WeakValueDictionary[int, asyncio.Lock]" = WeakValueDictionary()
_source_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()
_project_build_locks: "WeakValueDictionary[str, asyncio.Lock]" = (
  WeakValueDictionary()
)
_generated_file_cwd_locks: "WeakValueDictionary[str, asyncio.Lock]" = (
  WeakValueDictionary()
)
_lifecycle_lock = asyncio.Lock()
_shared_skills_lock = asyncio.Lock()
_log = logging.getLogger(__name__)


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


def project_build_lock(project_id: str) -> asyncio.Lock:
  """Serialize artifact builds within one project to one build at a time.

  A Möbius user pays for the CPU/RAM a build consumes on their own instance, so
  each project runs at most one build (heavy tectonic/website builds) at once.
  Held by ``project_builders.run_build`` across the whole build, so a second
  build of any artifact in the same project waits rather than running
  concurrently. This lock does NOT serialize the artifact-registry read-update-
  commit against it: those writes stay short and are ordered by the single
  event loop (all artifact-registry writers run on the loop with no await inside
  their read-update-commit), which is why the registry cannot lose an update
  even while a build holds this lock across its subprocess ``await``.

  Follows the per-app-lock pattern: a ``WeakValueDictionary`` so an idle lock
  garbage-collects itself, and the get-or-create is atomic from the event
  loop's point of view (no ``await`` between the lookup and the insert).
  """
  lock = _project_build_locks.get(project_id)
  if lock is None:
    lock = asyncio.Lock()
    _project_build_locks[project_id] = lock
  return lock


def source_dir_lock(source_dir: str) -> asyncio.Lock:
  """Serializes source_dir assignment with uninstall's source-tree cleanup.

  Held by explicit first apply / installer around assigning a source_dir +
  commit, and by ``delete_app`` around its shared-dir dedup check + ``rmtree``
  for the same directory, so a concurrent apply can't claim a directory in the
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


class CwdReservation:
  """A held generated-file cwd reservation. Call ``release()`` exactly once
  when the owning tool call's diff has finished; a second call is a no-op.
  See ``reserve_cwd_for_mutation`` for why this self-expires instead of
  relying solely on that call happening."""

  __slots__ = ("_lock", "_handle", "_released")

  def __init__(self, lock: asyncio.Lock, handle: asyncio.TimerHandle):
    self._lock = lock
    self._handle = handle
    self._released = False

  def release(self) -> None:
    if self._released:
      return
    self._released = True
    self._handle.cancel()
    try:
      self._lock.release()
    except RuntimeError:
      # Already unlocked (the watchdog fired first) — the outcome we want.
      pass

  @property
  def is_held(self) -> bool:
    """Whether this caller still owns the serialization window.

    A watchdog-expired reservation is deliberately not a license to publish
    from its old baseline: callers must fail closed instead.
    """
    return not self._released


async def reserve_cwd_for_mutation(
  cwd: str, *, acquire_timeout: float = 30.0, max_hold: float = 120.0,
) -> CwdReservation | None:
  """Best-effort serialization of one mutating tool call's diff window
  against every other chat's turn sharing the same cwd.

  Ordinary chats all run with cwd == settings.data_dir (a root shared by
  every other chat), so generated_files' before/after directory diff can
  observe a file a DIFFERENT, concurrently running chat's own tool call
  wrote directly at that shared root (not under either chat's own
  ``chats/<id>/`` namespace, which ``generated_files._belongs_to_other_chat``
  already excludes) and misattribute it. Holding this reservation for the
  span of one mutating tool call — acquired in a PreToolUse-equivalent hook,
  released once that call's diff completes — closes that remaining race for
  chats sharing the exact same cwd.

  This is deliberately NOT a plain lock the caller must remember to
  release correctly on every path (interrupts, provider crashes, a hook
  the SDK skips under some edge case): a permanently un-released lock
  would wedge every future chat sharing this cwd forever, which is a worse
  outage than the narrow leak this exists to close. So the reservation
  self-expires after ``max_hold`` seconds via an event-loop timer even if
  ``release()`` is never called, and acquiring gives up (returning
  ``None``, meaning "proceed unprotected") after ``acquire_timeout``
  seconds rather than blocking a turn indefinitely on another chat's
  reservation. Returns ``None`` on that timeout — the caller must treat a
  ``None`` result as "no reservation held" and continue anyway.
  """
  key = str(Path(cwd).resolve())
  lock = _generated_file_cwd_locks.get(key)
  if lock is None:
    lock = asyncio.Lock()
    _generated_file_cwd_locks[key] = lock
  try:
    await asyncio.wait_for(lock.acquire(), timeout=acquire_timeout)
  except TimeoutError:
    _log.warning(
      "generated-file cwd reservation timed out after %.0fs for %s; "
      "proceeding without cross-chat serialization for this tool call",
      acquire_timeout, key,
    )
    return None
  loop = asyncio.get_running_loop()
  reservation = CwdReservation.__new__(CwdReservation)
  reservation._lock = lock
  reservation._released = False
  reservation._handle = loop.call_later(max_hold, reservation.release)
  return reservation
