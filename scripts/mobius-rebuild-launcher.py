#!/usr/bin/env python3
"""Frozen root launcher for the self-hosted Möbius replacement worker.

Installed once as ``/usr/local/libexec/mobius-rebuild-host`` and never changed
by updates. It holds no replacement logic. It runs a copy of
``scripts/mobius-rebuild-host.py`` recorded in ``workers.json``:

- ``active``: the proven worker. It runs replacements without a candidate
  and reconciliation without a pinned trial recovery owner.
- ``candidate``: a newer worker the active one took from a verified official
  image after a successful replacement. The launcher removes it from the
  record before trying it on the next replacement, pinning it as the recovery
  owner. A trial the host interrupts is never repeated, but its unfinished
  transaction is reconciled by the same worker. It becomes active only when
  that replacement succeeds; the worker's revision high-water mark keeps it
  from ever being offered again otherwise.

Worker changes therefore ship in releases and activate without a host command,
and a faulty one costs one attempt. Only a change to this file needs a
reinstall (``deployment/self-hosted-helper.required``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

LAUNCHER_REVISION = 2
STATE_DIR = Path("/var/lib/mobius-rebuild")
WORKERS = STATE_DIR / "workers"
INDEX = STATE_DIR / "workers.json"
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "replace.lock"
TRIAL_LOCK = STATE_DIR / "candidate.lock"
TRANSACTION = STATE_DIR / "transaction.json"
PYTHON = "/usr/bin/python3"
ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LANG": "C.UTF-8",
    "MOBIUS_REBUILD_LAUNCHER": str(LAUNCHER_REVISION),
}


def _root_private(path: Path, *, directory: bool) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    kind = 0o040000 if directory else 0o100000
    return (
        info.st_mode & 0o170000 == kind and info.st_uid == 0
        and info.st_mode & 0o077 == 0
    )


def _worker(entry) -> dict | None:
    """One recorded worker, only if its file is private and unchanged."""
    try:
        path = WORKERS / str(entry["file"])
        digest = str(entry["sha256"])
    except (KeyError, TypeError):
        return None
    if path.parent != WORKERS or not _root_private(path, directory=False):
        return None
    try:
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            return None
    except OSError:
        return None
    return {**entry, "path": path}


def load_index() -> dict | None:
    if not (_root_private(WORKERS, directory=True)
            and _root_private(INDEX, directory=False)):
        return None
    try:
        index = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(index, dict) or index.get("version") != 1:
        return None
    active = _worker(index.get("active"))
    if active is None:
        return None
    candidate = _worker(index["candidate"]) if index.get("candidate") else None
    recovery_entry = index.get("recovery")
    recovery = _worker(recovery_entry) if recovery_entry is not None else None
    if recovery_entry is not None and recovery is None:
        return None  # never fall back to an incompatible active worker
    return {**index, "active": active, "candidate": candidate,
            "recovery": recovery}


def _record(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "path"}


def _update(change) -> None:
    """Apply ``change`` to the stored record under the replacement lock."""
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        stored = json.loads(INDEX.read_text(encoding="utf-8"))
        change(stored)
        fd, name = tempfile.mkstemp(dir=STATE_DIR, prefix=".workers.json.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(stored, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            os.replace(name, INDEX)
            directory = os.open(STATE_DIR, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)


def _status() -> bytes:
    try:
        return STATUS.read_bytes()
    except OSError:
        return b""


def _pending_transaction() -> bool:
    """Check journal presence only while holding replace.lock.

    An untrusted journal is not evidence that it is safe to discard the pin.
    """
    try:
        TRANSACTION.lstat()
    except FileNotFoundError:
        return False
    if not _root_private(TRANSACTION, directory=False):
        raise RuntimeError("untrusted replacement transaction")
    return True


def _same_worker(left: dict | None, right: dict) -> bool:
    return left == _record(right)


def _attributable_success(recovery: dict, after: bytes) -> bool:
    """Only a fresh outcome from this exact trial can prove promotion."""
    if hashlib.sha256(after).hexdigest() == recovery.get("trial_status_sha256"):
        return False
    try:
        status = json.loads(after)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(status, dict):
        return False
    operation = status.get("operation_id")
    return bool(
        status.get("state") in {"succeeded", "no_change"}
        and status.get("worker_revision") == recovery["revision"]
        and isinstance(operation, str) and len(operation) == 32
        and all(char in "0123456789abcdef" for char in operation)
        and operation != recovery.get("trial_operation_id")
    )


def _settle_recovery(stored: dict, recovery: dict) -> bool:
    """Settle an absent journal, including an exact attributable success."""
    if not _same_worker(stored.get("recovery"), recovery) or _pending_transaction():
        return False
    promoted = bool(
        (stored.get("active") or {}).get("sha256") == recovery.get("trial_replaced_sha")
        and _attributable_success(recovery, _status())
    )
    if promoted:
        stored["active"] = {key: value for key, value in _record(recovery).items()
                            if not key.startswith("trial_")}
    stored["recovery"] = None
    return promoted


def _clear_recovery(recovery: dict) -> None:
    def clear(stored):
        _settle_recovery(stored, recovery)

    _update(clear)


def recover_pinned(recovery: dict, dispatch_fd: int) -> tuple[int, bool]:
    """Resume only the pinned worker; never replay its original run."""
    pending = []
    promoted = []

    def inspect(stored):
        if not _same_worker(stored.get("recovery"), recovery):
            return
        if _pending_transaction():
            pending.append(True)
        else:
            if _settle_recovery(stored, recovery):
                promoted.append(True)

    _update(inspect)
    if not pending:
        return 0, bool(promoted)
    result = execute(recovery, "reconcile", trial_fd=dispatch_fd)
    _clear_recovery(recovery)
    return result, True


def execute(worker: dict, command: str, *, trial_fd: int | None = None) -> int:
    return subprocess.run(
        [PYTHON, "-I", "-S", str(worker["path"]), command],
        env=ENV, cwd="/", check=False,
        pass_fds=(trial_fd,) if trial_fd is not None else (),
    ).returncode


def try_candidate(candidate: dict, replaced: str, dispatch_fd: int) -> int:
    """Run one replacement with the candidate, taken out of the record first."""
    taken = []
    pinned = []
    before = []

    def take(stored):
        if (_same_worker(stored.get("candidate"), candidate)
                and not stored.get("recovery")
                and (stored.get("active") or {}).get("sha256") == replaced):
            stored["candidate"] = None
            baseline = _status()
            try:
                prior_operation = json.loads(baseline).get("operation_id")
            except (ValueError, AttributeError):
                prior_operation = None
            recovery = {**_record(candidate),
                        "trial_replaced_sha": replaced,
                        "trial_status_sha256": hashlib.sha256(baseline).hexdigest(),
                        "trial_operation_id": prior_operation}
            stored["recovery"] = recovery
            pinned.append(recovery)
            before.append(baseline)
            taken.append(True)

    _update(take)
    if not taken:
        return 1  # the record changed first; the next run reads it afresh
    # The child keeps the dispatch lock if the launcher is killed after
    # Popen but before the worker acquires replace.lock or journals.
    result = execute(candidate, "run", trial_fd=dispatch_fd)
    after = _status()
    def settle(stored):
        if not _same_worker(stored.get("recovery"), pinned[0]):
            return
        if _pending_transaction():
            return  # only this worker may finish its journal
        stored["recovery"] = None
        if (stored.get("active") or {}).get("sha256") != replaced:
            return  # a newer worker was installed while it ran
        if after == before[0] and result == 0:
            # Nothing was queued: the candidate has not been tried.
            if stored.get("high_water") == candidate["revision"] and not stored.get("candidate"):
                stored["candidate"] = _record(candidate)
        elif _attributable_success(pinned[0], after):
            stored["active"] = _record(candidate)

    _update(settle)
    return result


def main(argv: list[str]) -> int:
    os.umask(0o077)
    if (os.geteuid() != 0 or len(argv) != 2
            or argv[1] not in {"run", "reconcile"}):
        print("invalid invocation", file=sys.stderr)
        return 2
    # Selection, pinning and execution share one lifetime fence. The worker
    # inherits it so a killed launcher cannot admit a conflicting dispatch
    # before its child has reached (or finished) the transaction journal.
    with TRIAL_LOCK.open("a+") as dispatch:
        try:
            fcntl.flock(dispatch, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0 if argv[1] == "reconcile" else 1
        return _dispatch(argv[1], dispatch.fileno())


def _dispatch(command: str, dispatch_fd: int) -> int:
    index = load_index()
    if index is None:
        print("no verified replacement worker is installed; rerun "
              "scripts/install-rebuild-helper.sh", file=sys.stderr)
        return 1
    if index["recovery"]:
        result, reconciled = recover_pinned(index["recovery"], dispatch_fd)
        if result != 0 or reconciled:
            return result
        index = load_index()
        if index is None or index["recovery"]:
            return 0 if index is not None else 1
    if command == "run" and index["candidate"]:
        return try_candidate(index["candidate"], index["active"]["sha256"], dispatch_fd)
    return execute(index["active"], command, trial_fd=dispatch_fd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
