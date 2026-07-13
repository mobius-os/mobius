"""Tests for the read-only observability additions to routes/fs.py.

Covers the additive surfaces the Editor redesign relies on: the /disk mount
summary, tree?counts=1 directory child counts, read?head=1 big-file peeking,
and the /du recursive subtree size. Each asserts the default (param-absent)
behavior is unchanged, and /du asserts its bounds (deny-prune, symlink
non-follow) hold."""

import os
from pathlib import Path

import pytest

from app.config import get_settings


@pytest.fixture
def fsroot():
  """The dir the /api/fs viewer is rooted at (= data_dir in tests). Cleans up
  the test files (incl. root-level secret stand-ins) afterwards."""
  root = Path(get_settings().fs_view_root or get_settings().data_dir).resolve()
  work = root / "fsobstest"
  work.mkdir(parents=True, exist_ok=True)
  made: list[Path] = []
  yield root, work, made
  import shutil
  shutil.rmtree(work, ignore_errors=True)
  for p in made:
    if p.is_dir():
      shutil.rmtree(p, ignore_errors=True)
    else:
      p.unlink(missing_ok=True)


def test_disk_reports_positive_usage(client, auth, fsroot):
  r = client.get("/api/fs/disk", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert set(body) == {"total", "used", "free", "path"}
  assert body["total"] > 0 and body["used"] > 0 and body["free"] > 0
  # shutil.disk_usage's `used` excludes root-reserved blocks, so it is at most
  # total - free (the gap is the reserved space); used + free never exceeds
  # total.
  assert body["used"] <= body["total"] - body["free"]
  assert body["used"] + body["free"] <= body["total"]
  assert body["path"] == get_settings().data_dir


def test_disk_owner_required(client, fsroot):
  assert client.get("/api/fs/disk").status_code == 401


def test_tree_counts_adds_child_count_to_dirs_only(client, auth, fsroot):
  _, work, _ = fsroot
  (work / "sub").mkdir(exist_ok=True)
  (work / "a.txt").write_text("a")

  d = client.get("/api/fs/tree",
                 params={"path": "fsobstest", "counts": 1}, headers=auth).json()
  by_name = {e["name"]: e for e in d["entries"]}
  # Directory entries carry the badge; file entries never do.
  assert "child_count" in by_name["sub"]
  assert "child_count" not in by_name["a.txt"]


def test_tree_without_counts_has_no_child_count(client, auth, fsroot):
  # Regression guard: the default (param absent) response must be unchanged —
  # no child_count field on any entry.
  _, work, _ = fsroot
  (work / "sub").mkdir(exist_ok=True)
  (work / "a.txt").write_text("a")

  d = client.get("/api/fs/tree", params={"path": "fsobstest"}, headers=auth).json()
  assert all("child_count" not in e for e in d["entries"])
  # counts=0 explicitly is likewise unchanged.
  d0 = client.get("/api/fs/tree",
                  params={"path": "fsobstest", "counts": 0}, headers=auth).json()
  assert all("child_count" not in e for e in d0["entries"])


def test_tree_child_count_matches_seeded_files(client, auth, fsroot):
  _, work, _ = fsroot
  sub = work / "sub"
  sub.mkdir(exist_ok=True)
  for i in range(3):
    (sub / f"f{i}.txt").write_text("x")

  d = client.get("/api/fs/tree",
                 params={"path": "fsobstest", "counts": 1}, headers=auth).json()
  sub_entry = next(e for e in d["entries"] if e["name"] == "sub")
  assert sub_entry["child_count"] == 3


def test_read_head_small_file_returns_full_content(client, auth, fsroot):
  # head=1 on a file within the cap behaves exactly as today: full body, no
  # truncation header.
  _, work, _ = fsroot
  (work / "small.txt").write_text("hello\n")
  r = client.get("/api/fs/read",
                 params={"path": "fsobstest/small.txt", "head": 1}, headers=auth)
  assert r.status_code == 200
  assert r.text == "hello\n"
  assert "X-Mobius-Truncated" not in r.headers


def test_read_head_big_text_returns_truncated_prefix(client, auth, fsroot):
  # A >5 MB text file with head=1 returns the first 256 KB plus the truncation
  # headers, instead of a 413.
  _, work, _ = fsroot
  big = work / "big.log"
  size = 6 * 1024 * 1024
  big.write_text("a" * size)
  r = client.get("/api/fs/read",
                 params={"path": "fsobstest/big.log", "head": 1}, headers=auth)
  assert r.status_code == 200
  assert r.headers["X-Mobius-Truncated"] == "1"
  assert r.headers["X-Mobius-Total-Size"] == str(size)
  assert len(r.text) == 256 * 1024


def test_read_big_text_without_head_still_413(client, auth, fsroot):
  # Regression guard: without head=1 an oversized file still 413s.
  _, work, _ = fsroot
  big = work / "big.log"
  big.write_text("a" * (6 * 1024 * 1024))
  r = client.get("/api/fs/read",
                 params={"path": "fsobstest/big.log"}, headers=auth)
  assert r.status_code == 413


def test_read_head_big_binary_still_413(client, auth, fsroot):
  # head=1 never returns a partial binary — an oversized binary still 413s.
  _, work, _ = fsroot
  big = work / "big.bin"
  big.write_bytes(b"\x00" + b"a" * (6 * 1024 * 1024))
  r = client.get("/api/fs/read",
                 params={"path": "fsobstest/big.bin", "head": 1}, headers=auth)
  assert r.status_code == 413


def test_du_sums_known_tree(client, auth, fsroot):
  # A small nested tree of known byte sizes: du returns exact bytes/files/dirs
  # and truncated=false for a walk that completes fully.
  _, work, _ = fsroot
  (work / "a.txt").write_text("x" * 100)
  (work / "b.txt").write_text("x" * 50)
  sub = work / "sub"
  sub.mkdir(exist_ok=True)
  (sub / "c.txt").write_text("x" * 25)
  deep = sub / "deep"
  deep.mkdir(exist_ok=True)
  (deep / "d.txt").write_text("x" * 10)

  d = client.get("/api/fs/du",
                 params={"path": "fsobstest"}, headers=auth).json()
  assert d["path"] == "fsobstest"
  assert d["bytes"] == 185
  assert d["files"] == 4
  assert d["dirs"] == 2  # sub + deep; the path itself is never counted
  assert d["truncated"] is False


def test_du_owner_required(client, fsroot):
  assert client.get("/api/fs/du").status_code == 401


def test_du_missing_or_file_returns_zeros(client, auth, fsroot):
  # A non-directory or missing path returns zeros (like tree), not a 404.
  _, work, _ = fsroot
  (work / "a.txt").write_text("hello")
  zero = {"bytes": 0, "files": 0, "dirs": 0, "truncated": False}
  f = client.get("/api/fs/du",
                 params={"path": "fsobstest/a.txt"}, headers=auth).json()
  assert {k: f[k] for k in zero} == zero
  missing = client.get("/api/fs/du",
                       params={"path": "fsobstest/nope"}, headers=auth).json()
  assert {k: missing[k] for k in zero} == zero


def test_du_denied_subdir_is_pruned(client, auth, fsroot):
  # A deny-listed subtree — here a dir literally named `.env`, a secret name —
  # is pruned from both the byte total and the dir count, and the prune flips
  # truncated true because the reported total is now a lower bound.
  _, work, _ = fsroot
  (work / "normal.txt").write_text("x" * 40)
  secret = work / ".env"
  secret.mkdir(exist_ok=True)
  (secret / "leak.txt").write_text("x" * 9999)

  d = client.get("/api/fs/du",
                 params={"path": "fsobstest"}, headers=auth).json()
  assert d["bytes"] == 40  # only normal.txt; the .env subtree is excluded
  assert d["files"] == 1
  assert d["dirs"] == 0  # .env pruned, no other subdir
  assert d["truncated"] is True


def test_du_denied_path_directly_403(client, auth, fsroot):
  # Pointing du straight at a denied path 403s, like every other fs route.
  _, work, _ = fsroot
  (work / ".env").mkdir(exist_ok=True)
  r = client.get("/api/fs/du",
                 params={"path": "fsobstest/.env"}, headers=auth)
  assert r.status_code == 403


def test_du_does_not_follow_dir_symlink_loop(client, auth, fsroot):
  # A directory symlink pointing back at a parent would make a naive walk
  # recurse forever. followlinks=False plus pruning symlinked dirs terminates
  # the walk with the real, bounded counts; a symlink holds no real data here,
  # so it is neither followed nor counted and truncated stays false.
  _, work, _ = fsroot
  (work / "data.txt").write_text("x" * 30)
  real = work / "real"
  real.mkdir(exist_ok=True)
  (real / "inner.txt").write_text("x" * 5)
  os.symlink(work, work / "loop")  # a cycle straight back to the subtree root

  d = client.get("/api/fs/du",
                 params={"path": "fsobstest"}, headers=auth).json()
  assert d["bytes"] == 35  # data.txt + real/inner.txt; the loop is not entered
  assert d["files"] == 2
  assert d["dirs"] == 1  # `real` only — the `loop` symlink is not counted
  assert d["truncated"] is False
