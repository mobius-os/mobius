#!/usr/bin/env python3
"""Reconcile platform-owned skills at boot without a hand-maintained digest list.

The sidecar records the last platform version applied to each flat skill. Local
edits or deletions remain in place and are marked for review. An installation
predating the sidecar is adopted only when its bytes match a seed blob in the
image revision's Git ancestry; uncertain copies are never overwritten. A
retired seed is archived outside discovery before its active path is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

_APP_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if _APP_IMPORT_ROOT not in sys.path:
  sys.path.insert(0, _APP_IMPORT_ROOT)

from app.storage_io import atomic_write  # noqa: E402

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SKILLS = DATA_DIR / "shared" / "skills"
RETIRED_SKILLS = DATA_DIR / "shared" / "retired-skills"
PLATFORM_REPO = DATA_DIR / "platform"
BUILD_SHA = os.environ.get("BUILD_SHA", "")
SIDECAR = ".seed-skills.json"
SEED_PATH = "backend/scripts/seed-skills"
_SEED_CANDIDATES = (
  Path("/app/scripts/seed-skills"),
  Path(__file__).resolve().parent / "seed-skills",
)

# Two exact owner-curated legacy copies were deliberately migrated by the old
# registry because their instructions crossed a safety boundary. They are not
# recoverable from seed Git history: keep this narrow compatibility exception,
# archive the exact bytes first, and never generalize it into an update list.
# platform-maintenance omitted the Host-owned container cutover boundary;
# cron told app jobs to read the owner service token instead of APP_TOKEN.
_UNSAFE_LEGACY_COPIES = {
  "platform-maintenance.md": "668bd365e2edf694c921606c9619fff7b8e58806a9eb48745058b22731c44995",
  "cron.md": "16055ea6ba6e4663636f87fde9868aa98d49ab39c5037ff90fa673d96c259cd9",
}


def _sha(content: bytes) -> str:
  return hashlib.sha256(content).hexdigest()


def _valid_digest(value: object) -> bool:
  return (
    isinstance(value, str) and len(value) == 64
    and all(c in "0123456789abcdef" for c in value)
  )


def _plain_file(path: Path) -> bool:
  try:
    return stat.S_ISREG(path.lstat().st_mode)
  except FileNotFoundError:
    return False


def _write_records(records: dict) -> None:
  path = SKILLS / SIDECAR
  atomic_write(path, json.dumps(records, indent=2, sort_keys=True) + "\n")
  _writable(path)


def _writable(path: Path) -> None:
  try:
    mobius = pwd.getpwnam("mobius")
  except KeyError:
    return
  try:
    os.chown(path, mobius.pw_uid, mobius.pw_gid)
    os.chmod(path, 0o775 if path.is_dir() else 0o664)
  except OSError:
    pass


def _read_records(path: Path, *, platform: bool = False) -> dict | None:
  if path.is_symlink():
    return None
  if not path.exists():
    return {}
  if not _plain_file(path):
    return None
  try:
    records = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  if not isinstance(records, dict):
    return None
  for name, record in records.items():
    if (
      not isinstance(name, str) or Path(name).name != name
      or not name.endswith(".md") or not isinstance(record, dict)
    ):
      return None
    if platform:
      baseline = record.get("baseline_sha256")
      upstream = record.get("upstream_sha256")
      status = record.get("status")
      if (
        (baseline is not None and not _valid_digest(baseline))
        or (upstream is not None and not _valid_digest(upstream))
        or status not in {"current", "needs_review", "missing_local", "held", "retired"}
        or (status == "retired") != (upstream is None)
      ):
        return None
  return records


def _git_history() -> dict[str, set[str]] | None:
  """Historical seed bytes reachable from this image's exact source revision.

  The image's baked checkout may be shallow. The persistent platform checkout
  carries history, but may also have advanced past the image. Pin to BUILD_SHA
  so a future-only or locally-authored seed cannot authorize a boot rewrite.
  """
  if len(BUILD_SHA) != 40 or any(c not in "0123456789abcdef" for c in BUILD_SHA):
    return None
  if not (PLATFORM_REPO / ".git").exists():
    return None
  env = {k: v for k, v in os.environ.items() if k not in {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR", "GIT_NAMESPACE",
  }}
  base = ["git", "-c", f"safe.directory={PLATFORM_REPO}", "-C", str(PLATFORM_REPO)]
  try:
    listed = subprocess.run(
      [*base, "log", "--raw", "--no-abbrev", "--no-renames", "--format=", BUILD_SHA, "--", SEED_PATH],
      capture_output=True, check=True, timeout=30, env=env,
    ).stdout
    objects: list[tuple[str, str]] = []
    for line in listed.decode("utf-8").splitlines():
      meta, _, rel = line.partition("\t")
      if rel.startswith(SEED_PATH + "/") and rel.endswith(".md"):
        name = Path(rel).name
        if rel == f"{SEED_PATH}/{name}":
          parts = meta.split()
          if len(parts) != 5:
            return None
          for oid in parts[2:4]:
            if oid != "0" * 40:
              objects.append((oid, name))
    objects = list(dict.fromkeys(objects))
    if not objects:
      return None
    batch = subprocess.run(
      [*base, "cat-file", "--batch"],
      input=("\n".join(oid for oid, _ in objects) + "\n").encode(),
      capture_output=True, check=True, timeout=30, env=env,
    ).stdout
  except (OSError, subprocess.SubprocessError, UnicodeError):
    return None
  history: dict[str, set[str]] = {}
  offset = 0
  for oid, name in objects:
    end = batch.find(b"\n", offset)
    if end < 0:
      return None
    header = batch[offset:end].split()
    if len(header) != 3 or header[0] != oid.encode() or header[1] != b"blob":
      return None
    try:
      size = int(header[2])
    except ValueError:
      return None
    offset = end + 1
    if size < 0 or batch[offset + size:offset + size + 1] != b"\n":
      return None
    history.setdefault(name, set()).add(_sha(batch[offset:offset + size]))
    offset += size + 1
  return history if offset == len(batch) else None


def _archive(name: str, content: bytes) -> bool:
  archive = RETIRED_SKILLS / f"{Path(name).stem}-{_sha(content)}.md"
  try:
    RETIRED_SKILLS.mkdir(parents=True, exist_ok=True)
    _writable(RETIRED_SKILLS)
    if archive.exists():
      if not _plain_file(archive) or archive.read_bytes() != content:
        return False
    else:
      atomic_write(archive, content)
      _writable(archive)
    return archive.read_bytes() == content
  except OSError:
    return False


def _write_index() -> None:
  try:
    from app.skills import write_index
    write_index(SKILLS)
  except Exception as exc:  # index is an inspection convenience, not boot-critical
    print(f"init_skills: index generation skipped ({exc})")


def init() -> None:
  seed = next((path for path in _SEED_CANDIDATES if path.is_dir()), None)
  if seed is None:
    print("init_skills: no seed-skills dir found; skipping")
    return
  SKILLS.mkdir(parents=True, exist_ok=True)
  _writable(SKILLS)
  records = _read_records(SKILLS / SIDECAR, platform=True)
  app_records = _read_records(SKILLS / ".app-skills.json")
  if records is None or app_records is None:
    print("init_skills: ownership sidecar unreadable; no seed changes made")
    return
  try:
    from app.skills import load_installed_sidecar, reconcile_installed
    reconcile_installed(SKILLS)
    installed = load_installed_sidecar(SKILLS)
  except Exception as exc:
    print(f"init_skills: installed skill ownership unreadable; no seed changes made ({exc})")
    return
  history = _git_history()
  if history is None and not (SKILLS / SIDECAR).exists():
    print("init_skills: seed history unavailable; existing skills will remain untouched")
  candidates = list(seed.glob("*.md"))
  if any(not _plain_file(path) for path in candidates):
    print("init_skills: seed tree contains an unexpected file type; no seed changes made")
    return
  seed_files = {path.name: path for path in candidates}
  app_owned = {name for name, rec in app_records.items() if isinstance(rec, dict)}
  installed_owned = {name for name, rec in installed.items() if isinstance(rec, dict)}

  # A removed platform seed leaves discovery, but its exact bytes are always
  # archived first. App-owned names are outside this lifecycle even if a prior
  # platform generation used the same basename.
  retired_names = (set(records) | set(history or {})) - set(seed_files)
  for name in sorted(retired_names):
    if records.get(name, {}).get("status") == "retired":
      continue
    if name in app_owned or Path(name).stem in installed_owned:
      records.pop(name, None)
      _write_records(records)
      continue
    target = SKILLS / name
    if target.is_symlink() or (target.exists() and not _plain_file(target)):
      print(f"init_skills: retired {name} has unexpected file type; left untouched")
      continue
    if _plain_file(target):
      content = target.read_bytes()
      if not _archive(name, content):
        print(f"init_skills: could not archive retired {name}; left untouched")
        continue
      target.unlink()
      print(f"init_skills: archived retired {name}")
    records[name] = {"baseline_sha256": None, "upstream_sha256": None, "status": "retired"}
    _write_records(records)

  for name, source in sorted(seed_files.items()):
    if name in app_owned or Path(name).stem in installed_owned or (SKILLS / Path(name).stem).exists():
      print(f"init_skills: {name} belongs to another skill owner; skipped")
      continue
    target = SKILLS / name
    upstream = source.read_bytes()
    upstream_sha = _sha(upstream)
    record = records.get(name)
    if target.is_symlink() or (target.exists() and not _plain_file(target)):
      print(f"init_skills: {name} has unexpected file type; left untouched")
      continue
    if target.exists():
      current = target.read_bytes()
      current_sha = _sha(current)
      if current_sha == _UNSAFE_LEGACY_COPIES.get(name):
        if not _archive(name, current):
          print(f"init_skills: could not preserve unsafe legacy {name}; left untouched")
          continue
        atomic_write(target, upstream)
        _writable(target)
        records[name] = {"baseline_sha256": upstream_sha, "upstream_sha256": upstream_sha, "status": "current"}
        _write_records(records)
        print(f"init_skills: preserved and replaced unsafe legacy {name}")
        continue
    if record is None and not target.exists():
      atomic_write(target, upstream)
      _writable(target)
      records[name] = {"baseline_sha256": upstream_sha, "upstream_sha256": upstream_sha, "status": "current"}
    elif record is None:
      baseline = current_sha if current_sha in (history or {}).get(name, set()) else None
      record = {"baseline_sha256": baseline, "upstream_sha256": upstream_sha}
      records[name] = record
    if record is not None:
      record["upstream_sha256"] = upstream_sha
      if not target.exists():
        record["status"] = "missing_local"
      else:
        current_sha = _sha(target.read_bytes())
        if current_sha == upstream_sha:
          record["baseline_sha256"] = upstream_sha
          record["status"] = "current"
        elif record.get("status") == "held":
          pass
        elif current_sha == record.get("baseline_sha256"):
          atomic_write(target, upstream)
          _writable(target)
          record["baseline_sha256"] = upstream_sha
          record["status"] = "current"
        else:
          record["status"] = "needs_review"
          print(f"init_skills: {name} has local changes; kept for review")
    _write_records(records)
  _write_index()


def resolve(name: str, decision: str, expected_sha256: str) -> None:
  """Record an explicit review decision without editing the owner copy blindly."""
  if Path(name).name != name or not name.endswith(".md"):
    raise ValueError("expected one platform skill filename")
  records = _read_records(SKILLS / SIDECAR, platform=True)
  if records is None or not isinstance(records.get(name), dict):
    raise ValueError("no readable platform ownership record for this skill")
  seed = next((path for path in _SEED_CANDIDATES if path.is_dir()), None)
  source = seed / name if seed is not None else None
  if source is None or not _plain_file(source):
    raise ValueError("platform seed is unavailable; nothing changed")
  upstream = source.read_bytes()
  record = records[name]
  if _sha(upstream) != record.get("upstream_sha256"):
    raise ValueError("image seed changed since review; reconcile before deciding")
  target = SKILLS / name
  if not _plain_file(target):
    raise ValueError("active skill is missing or not a plain file")
  current = target.read_bytes()
  if _sha(current) != expected_sha256:
    raise ValueError("skill changed since review; nothing changed")
  if decision == "keep-local":
    record["status"] = "held"
  elif decision == "take-upstream":
    if current != upstream and not _archive(name, current):
      raise OSError("could not preserve local skill bytes; nothing changed")
    atomic_write(target, upstream)
    _writable(target)
    record["baseline_sha256"] = _sha(upstream)
    record["status"] = "current"
  else:
    raise ValueError("decision must be keep-local or take-upstream")
  _write_records(records)
  _write_index()


if __name__ == "__main__":
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--resolve", metavar="SKILL.md")
  parser.add_argument("--decision", choices=("keep-local", "take-upstream"))
  parser.add_argument("--expected-sha256")
  args = parser.parse_args()
  if args.resolve:
    if not args.decision or not args.expected_sha256:
      parser.error("--decision and --expected-sha256 are required with --resolve")
    resolve(args.resolve, args.decision, args.expected_sha256)
  elif args.decision or args.expected_sha256:
    parser.error("--decision and --expected-sha256 require --resolve")
  else:
    init()
