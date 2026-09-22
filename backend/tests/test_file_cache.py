import mmap
import os
from pathlib import Path

from app import file_cache


def test_reclaim_file_cache_skips_mapped_files(tmp_path, monkeypatch):
  mapped_path = tmp_path / "mapped.bin"
  idle_path = tmp_path / "idle.bin"
  mapped_path.write_bytes(b"m" * 4096)
  idle_path.write_bytes(b"i" * 4096)
  calls = []
  monkeypatch.setattr(os, "posix_fadvise", lambda fd, offset, length, advice: calls.append(os.fstat(fd).st_ino))

  with mapped_path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0):
    result = file_cache.reclaim_file_cache([tmp_path])

  assert result["files"] == 1
  assert result["skipped_mapped"] == 1
  assert calls == [idle_path.stat().st_ino]


def test_reclaim_file_cache_can_advise_mapped_provider_file(tmp_path, monkeypatch):
  mapped_path = tmp_path / "provider.bin"
  mapped_path.write_bytes(b"p" * 4096)
  calls = []
  monkeypatch.setattr(
    os,
    "posix_fadvise",
    lambda fd, offset, length, advice: calls.append(os.fstat(fd).st_ino),
  )

  with mapped_path.open("r+b") as handle, mmap.mmap(handle.fileno(), 0):
    result = file_cache.reclaim_file_cache(
      [mapped_path], skip_mapped=False,
    )

  assert result["files"] == 1
  assert result["skipped_mapped"] == 0
  assert calls == [mapped_path.stat().st_ino]


def test_provider_tool_paths_rejects_unknown_provider():
  assert file_cache.provider_tool_paths("other") == ()


def test_settled_turn_paths_excludes_owner_data_and_credentials(tmp_path):
  (tmp_path / "platform" / ".git").mkdir(parents=True)
  (tmp_path / "platform" / "frontend" / "node_modules").mkdir(parents=True)
  (tmp_path / "contrib" / "candidate" / ".git").mkdir(parents=True)
  (tmp_path / "apps" / "example" / ".git").mkdir(parents=True)
  paths = file_cache.settled_turn_paths(tmp_path, "chat-1")

  assert tmp_path / "platform" / ".git" in paths
  assert tmp_path / "platform" / "backend" in paths
  assert tmp_path / "platform" / "frontend" / "dist" in paths
  assert tmp_path / "contrib" / "candidate" / ".git" in paths
  assert tmp_path / "apps" / "example" / ".git" in paths
  assert Path("/usr") not in paths
  assert Path("/opt") not in paths
  assert all("cli-auth" not in str(path) for path in paths)
  assert all(str(tmp_path / "db") not in str(path) for path in paths)
