#!/usr/bin/env python3
"""Root-pinned pre-entrypoint gate for one host-owned cutover operation.

The host verifies Docker identities and asserts exact-resource quiescence. This
module does not invoke Docker. Each operation has its own persistent state
mount, outside /data and its legacy ownership sweep. All callers use this API;
legacy restart_ledger.py remains untouched and executes only after admission.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import secrets
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class AdmissionError(RuntimeError):
    """No permission to run legacy bootstrap or mutate its ledger."""


_TOKEN = re.compile(r"[A-Za-z0-9._:-]{8,160}\Z")
_CID = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_LIMIT = 1024 * 1024


def _require(condition, message):
    if not condition:
        raise AdmissionError(message)


def _token(value):
    return isinstance(value, str) and bool(_TOKEN.fullmatch(value))


def _cid(value):
    return isinstance(value, str) and bool(_CID.fullmatch(value))


def _image(value):
    return isinstance(value, str) and bool(_IMAGE.fullmatch(value))


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def nonfinite(_value):
        raise AdmissionError("nonfinite JSON")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


class AdmissionStore:
    def __init__(self, state_dir, data_dir, trusted_uid=0, trusted_gid=0):
        self.state_dir = Path(state_dir)
        self.data_dir = Path(data_dir)
        self.uid, self.gid = trusted_uid, trusted_gid

    def _trusted(self, info, *, directory=False):
        _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                 and info.st_uid == self.uid and info.st_gid == self.gid
                 and not info.st_mode & 0o022
                 and (directory or info.st_nlink == 1), "untrusted admission/ledger inode")

    def _directory(self, path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._trusted(os.fstat(fd), directory=True)
            return fd
        except BaseException:
            os.close(fd)
            raise

    @contextmanager
    def _locked(self):
        # Only the host prepare path creates the directory. Consumers fail
        # closed when their mount/state is missing instead of inventing it.
        directory = self._directory(self.state_dir)
        lock = None
        try:
            lock = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                           0o600, dir_fd=directory)
            self._trusted(os.fstat(lock))
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._trusted(os.fstat(lock))
            current = self.state_dir.lstat()
            opened = os.fstat(directory)
            _require((current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino),
                     "admission directory changed")
            linked = os.stat("lock", dir_fd=directory, follow_symlinks=False)
            held = os.fstat(lock)
            _require((linked.st_dev, linked.st_ino) == (held.st_dev, held.st_ino), "lock replaced")
            os.fsync(directory)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _read(self, directory, name, *, text=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            self._trusted(info)
            _require(info.st_size <= _LIMIT, "oversized trusted record")
            raw = os.read(fd, _LIMIT + 1)
            _require(len(raw) <= _LIMIT, "oversized trusted record")
            return raw.decode().strip() if text else _json(raw)
        except (ValueError, UnicodeError) as exc:
            raise AdmissionError("malformed trusted record") from exc
        finally:
            os.close(fd)

    def _write(self, directory, name, value, mode=0o600):
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        _require(len(payload) <= _LIMIT, "operation history is full")
        temporary = f".{name}.{secrets.token_hex(12)}"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode, dir_fd=directory)
            try:
                self._trusted(os.fstat(fd))
                os.fchmod(fd, mode)
                with os.fdopen(fd, "wb", closefd=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _receipt(self, value, operation, source_boot):
        _require(isinstance(value, dict) and value.get("version") == 1
                 and value.get("action") == "external_cutover"
                 and value.get("cutover_id") == operation
                 and value.get("source_boot_id") == source_boot
                 and _token(value.get("nonce"))
                 and _number(value.get("accepted_at"))
                 and _number(value.get("created_at")), "invalid original cutover receipt")

    def _state(self, directory):
        value = self._read(directory, "state.json")
        _require(isinstance(value, dict) and value.get("version") == 1
                 and _token(value.get("operation")) and isinstance(value.get("source"), dict)
                 and isinstance(value.get("slots"), dict)
                 and set(value["slots"]) == {"target", "rollback"}
                 and isinstance(value.get("quiesced"), list), "missing/malformed operation")
        source = value["source"]
        _require(_cid(source.get("cid")) and _image(source.get("image"))
                 and _token(source.get("boot_id")), "invalid source identity")
        self._receipt(value.get("receipt"), value["operation"], source["boot_id"])
        _require("source_handoff_valid" in value and (
            type(value["source_handoff_valid"]) is bool if source["cid"] in value["quiesced"]
            else value["source_handoff_valid"] is None), "invalid source handoff snapshot")
        tokens, cids = set(), {source["cid"]}
        for role, slot in value["slots"].items():
            _require(isinstance(slot, dict) and isinstance(slot.get("attempts"), list)
                     and "consumed" in slot, "invalid slot")
            admitted = []
            for generation, attempt in enumerate(slot["attempts"], 1):
                _require(isinstance(attempt, dict) and type(attempt.get("generation")) is int and attempt["generation"] == generation
                         and _token(attempt.get("token")) and attempt["token"] not in tokens
                         and _image(attempt.get("image"))
                         and attempt.get("state") in {"allocated", "open", "closed", "admitted"}
                         and type(attempt.get("issued")) is bool
                         and type(attempt.get("fenced")) is bool,
                         "invalid attempt")
                _require(role != "rollback" or attempt["image"] == source["image"], "rollback image changed")
                if role == "target" and generation > 1:
                    _require(attempt["image"] == slot["attempts"][0]["image"], "target image changed")
                tokens.add(attempt["token"])
                cid = attempt.get("cid")
                _require(cid is None or (_cid(cid) and cid not in cids), "invalid/reused container identity")
                if cid:
                    cids.add(cid)
                _require(attempt["state"] not in {"open", "admitted"} or cid is not None,
                         "unbound open attempt")
                if attempt["state"] == "admitted":
                    _require(_token(attempt.get("boot_id")) and attempt["issued"]
                             and type(attempt.get("continuation")) is bool, "invalid claimed boot")
                    admitted.append({"generation": generation, "boot_id": attempt["boot_id"]})
                _require(not attempt["fenced"] or (attempt["state"] == "closed"
                         and cid is not None and cid in value["quiesced"]), "invalid fence")
                if generation < len(slot["attempts"]):
                    _require(attempt["state"] == "closed" and attempt["fenced"], "unfenced predecessor")
            consumed = slot.get("consumed")
            _require((not admitted and consumed is None) or (len(admitted) == 1 and consumed == admitted[0]),
                     "invalid consumption history")
        _require(all(_cid(cid) and cid in cids for cid in value["quiesced"]), "invalid quiescence history")
        return value

    def _save(self, directory, value):
        self._write(directory, "state.json", value)

    def prepare(self, operation, receipt, source_cid, source_image, source_boot_id):
        _require(_token(operation) and _cid(source_cid) and _image(source_image)
                 and _token(source_boot_id), "invalid operation/source identity")
        self._receipt(receipt, operation, source_boot_id)
        source = {"cid": source_cid, "image": source_image, "boot_id": source_boot_id}
        try:
            self.state_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        # Also sync on replay: a previous mkdir may have survived an fsync
        # error in memory without its parent entry becoming durable yet.
        parent = self._directory(self.state_dir.parent)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        with self._locked() as directory:
            try:
                os.stat("state.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
            else:
                exists = True
            if exists:
                state = self._state(directory)
                _require(state["operation"] == operation and state["source"] == source
                         and state["receipt"] == receipt, "operation already prepared differently")
                return state
            ledger = self._directory(self.data_dir / ".restart-ledger")
            try:
                _require(self._read(ledger, "cutover-receipt.json") == receipt,
                         "source receipt not witnessed")
            finally:
                os.close(ledger)
            state = {"version": 1, "operation": operation, "receipt": receipt, "source": source,
                     "source_handoff_valid": None, "quiesced": [], "slots": {role: {"consumed": None, "attempts": []}
                                                for role in ("target", "rollback")}}
            self._save(directory, state)
            return state

    def observe(self):
        with self._locked() as directory:
            return self._state(directory)

    @staticmethod
    def _attempt(state, role, token):
        _require(role in ("target", "rollback"), "unknown role")
        for attempt in state["slots"][role]["attempts"]:
            if attempt["token"] == token:
                return attempt
        raise AdmissionError("unknown attempt")

    @staticmethod
    def _quiescent(state, role):
        _require(state["source"]["cid"] in state["quiesced"], "source not quiesced")
        if role == "rollback":
            _require(all(a["cid"] is not None and a["cid"] in state["quiesced"]
                         for a in state["slots"]["target"]["attempts"]), "target not quiesced")

    def quiesce(self, cid):
        """Host assertion: exact source/target CID has been removed, not just stopped."""
        with self._locked() as directory:
            state = self._state(directory)
            known = cid == state["source"]["cid"] or any(
                a["cid"] == cid and a["state"] in {"closed", "admitted"}
                for a in state["slots"]["target"]["attempts"])
            _require(known, "cannot quiesce this container")
            if cid not in state["quiesced"]:
                if cid == state["source"]["cid"]:
                    # The host asserts exact removal BEFORE this snapshot.
                    # A legacy source may already have stolen/partly consumed
                    # its acceptance. Receipt alone never repairs that loss.
                    ledger = self._directory(self.data_dir / ".restart-ledger")
                    try:
                        state["source_handoff_valid"] = self._original_evidence(
                            state, self._read(ledger, "accepted.json"), self._read(ledger, "ack.json"),
                            self._read(ledger, "boot-id", text=True))
                    finally:
                        os.close(ledger)
                state["quiesced"].append(cid)
                self._save(directory, state)

    def allocate(self, role, token, image):
        _require(role in ("target", "rollback") and _token(token) and _image(image), "invalid allocation")
        with self._locked() as directory:
            state = self._state(directory)
            for other_role, other_slot in state["slots"].items():
                for attempt in other_slot["attempts"]:
                    if attempt["token"] == token:
                        _require(other_role == role and attempt["image"] == image, "token already allocated")
                        return attempt
            slot = state["slots"][role]
            _require(slot["consumed"] is None, "slot already consumed")
            if role == "rollback":
                _require(image == state["source"]["image"], "rollback must use exact source image")
                self._quiescent(state, role)
            else:
                _require(not state["slots"]["rollback"]["attempts"], "rollback already selected")
                if slot["attempts"]:
                    _require(image == slot["attempts"][0]["image"], "target image changed")
            _require(not slot["attempts"] or (slot["attempts"][-1]["state"] == "closed"
                                              and slot["attempts"][-1]["fenced"]), "predecessor not fenced")
            attempt = {"generation": len(slot["attempts"]) + 1, "token": token, "image": image,
                       "cid": None, "state": "allocated", "issued": False, "fenced": False}
            slot["attempts"].append(attempt)
            self._save(directory, state)
            return attempt

    def bind(self, role, token, cid):
        _require(_cid(cid), "full container ID required")
        with self._locked() as directory:
            state = self._state(directory)
            attempt = self._attempt(state, role, token)
            _require(attempt["cid"] in (None, cid), "container already bound differently")
            if attempt["cid"] == cid:
                return
            _require(attempt["state"] in {"allocated", "closed"} and cid != state["source"]["cid"]
                     and not any(a["cid"] == cid for slot in state["slots"].values()
                                 for a in slot["attempts"]), "container identity already used")
            attempt["cid"] = cid
            self._save(directory, state)

    def open(self, role, token):
        with self._locked() as directory:
            state = self._state(directory)
            attempt = self._attempt(state, role, token)
            _require(state["slots"][role]["consumed"] is None and attempt["cid"] is not None
                     and attempt["state"] in {"allocated", "open"}, "attempt cannot open")
            self._quiescent(state, role)
            _require(attempt["cid"] not in state["quiesced"], "container already quiesced")
            attempt["state"] = "open"
            self._save(directory, state)

    def issue_start(self, role, token):
        with self._locked() as directory:
            state = self._state(directory)
            attempt = self._attempt(state, role, token)
            _require(attempt["state"] == "open" and state["slots"][role]["consumed"] is None,
                     "attempt cannot start")
            if attempt["issued"]:
                return False
            attempt["issued"] = True
            self._save(directory, state)
            return True

    def close_pending(self, role, token):
        with self._locked() as directory:
            state = self._state(directory)
            attempt = self._attempt(state, role, token)
            if state["slots"][role]["consumed"] is not None:
                return False
            _require(attempt["state"] in {"allocated", "open", "closed"}, "attempt cannot close")
            attempt["state"] = "closed"
            self._save(directory, state)
            return True

    def fenced(self, role, token, cid):
        with self._locked() as directory:
            state = self._state(directory)
            attempt = self._attempt(state, role, token)
            _require(attempt["state"] == "closed" and _cid(cid) and attempt["cid"] == cid,
                     "only exact closed container may be fenced")
            attempt["fenced"] = True
            if cid not in state["quiesced"]:
                state["quiesced"].append(cid)
            self._save(directory, state)

    @staticmethod
    def _matches(value, state):
        return (isinstance(value, dict) and value.get("version") == 1
                and value.get("action") == "external_cutover"
                and value.get("cutover_id") == state["operation"]
                and value.get("nonce") == state["receipt"]["nonce"])

    @classmethod
    def _original_evidence(cls, state, accepted, ack, boot):
        return bool(cls._matches(accepted, state)
                    and accepted.get("source_boot_id") == state["source"]["boot_id"]
                    and accepted.get("created_at") == state["receipt"]["created_at"]
                    and _number(accepted.get("accepted_at"))
                    and boot == state["source"]["boot_id"] and not cls._matches(ack, state))

    @classmethod
    def _handoff_evidence(cls, state, role, accepted, ack, boot):
        if state["source_handoff_valid"] is not True:
            return False
        if cls._original_evidence(state, accepted, ack, boot):
            return True
        if role != "rollback":
            return False
        target = state["slots"]["target"]
        if not target["consumed"] or not target["attempts"][-1].get("continuation"):
            return False
        target_boot = target["consumed"]["boot_id"]
        if boot != target_boot:
            return False
        exact_ack = (cls._matches(ack, state) and ack.get("target_boot_id") == target_boot
                     and ack.get("source_boot_id") == state["source"]["boot_id"])
        if accepted is not None:
            return bool(cls._matches(accepted, state) and accepted.get("source_boot_id") == target_boot
                        and (not cls._matches(ack, state) or exact_ack))
        return bool(exact_ack)

    def _retire_matching(self, ledger, state, *, all_external_acceptance=False):
        for name in ("accepted.json", "ack.json"):
            value = self._read(ledger, name)
            foreign_cutover = (all_external_acceptance and name == "accepted.json"
                               and isinstance(value, dict) and value.get("version") == 1
                               and value.get("action") == "external_cutover")
            if foreign_cutover:
                _require(_token(value.get("cutover_id")) and _token(value.get("source_boot_id")),
                         "malformed external acceptance")
                self._receipt(value, value["cutover_id"], value["source_boot_id"])
            if self._matches(value, state) or foreign_cutover:
                os.unlink(name, dir_fd=ledger)
                os.fsync(ledger)

    def enter(self, token, boot_id, *, now=None):
        """Return continuation/service_only; denial raises before touching /data."""
        _require(_token(token) and _token(boot_id), "invalid attempt/boot token")
        current = time.time() if now is None else now
        _require(_number(current), "invalid current time")
        with self._locked() as directory:
            state = self._state(directory)
            found = [(role, a) for role, slot in state["slots"].items()
                     for a in slot["attempts"] if a["token"] == token]
            _require(len(found) == 1, "unknown attempt")
            role, attempt = found[0]
            _require(attempt["state"] in {"open", "admitted"} and attempt["issued"]
                     and attempt["cid"] not in state["quiesced"], "attempt denied")
            self._quiescent(state, role)
            ledger = self._directory(self.data_dir / ".restart-ledger")
            try:
                if attempt["state"] == "admitted":
                    # This container may be the source of a later cutover.
                    # Its crash restart must not steal that replacement's
                    # acceptance. Keep the receipt for the new gate; ordinary
                    # independently authorized restarts remain untouched.
                    self._retire_matching(ledger, state, all_external_acceptance=True)
                    return "service_only"
                _require(state["slots"][role]["consumed"] is None, "slot consumed")
                receipt = state["receipt"]
                original = self._read(ledger, "cutover-receipt.json")
                _require(original == receipt, "original receipt changed or missing")
                existing = self._read(ledger, "accepted.json")
                _require(existing is None or self._matches(existing, state), "unrelated acceptance pending")
                prior_boot = self._read(ledger, "boot-id", text=True)
                ack = self._read(ledger, "ack.json")
                if existing is not None:
                    _require(_token(existing.get("source_boot_id")), "invalid acceptance source")
                    self._receipt(existing, state["operation"], existing["source_boot_id"])
                eligible = bool(_token(prior_boot) and prior_boot != boot_id
                                and receipt["accepted_at"] <= current + 5
                                and current - receipt["accepted_at"] <= 3600
                                and self._handoff_evidence(state, role, existing, ack, prior_boot))
                if eligible:
                    self._write(ledger, "accepted.json", {**receipt, "source_boot_id": prior_boot,
                                                          "accepted_at": current})
                else:
                    self._retire_matching(ledger, state)
                attempt.update(state="admitted", boot_id=boot_id, continuation=eligible)
                state["slots"][role]["consumed"] = {"generation": attempt["generation"], "boot_id": boot_id}
                self._save(directory, state)
                return "continuation" if eligible else "service_only"
            finally:
                os.close(ledger)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["enter"])
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--token", required=True)
    # Split ourselves: original argv is never interpreted as wrapper options.
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        boundary = args.index("--")
    except ValueError:
        parser.error("preserved original argv must follow --")
    options = parser.parse_args(args[:boundary])
    original = args[boundary + 1:]
    if not original:
        parser.error("missing original argv")
    boot_id = secrets.token_urlsafe(24)
    try:
        AdmissionStore(options.state_dir, options.data_dir).enter(options.token, boot_id)
    except (AdmissionError, OSError, ValueError) as exc:
        print(f"boot admission denied: {exc}", file=sys.stderr)
        return 75
    os.environ["MOBIUS_BOOT_ID"] = boot_id
    os.execvp(original[0], original)


if __name__ == "__main__":
    sys.exit(main())
