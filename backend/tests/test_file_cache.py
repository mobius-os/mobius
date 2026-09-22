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


def test_advised_size_is_not_reported_as_reclaimed_memory(tmp_path, monkeypatch):
  path = tmp_path / 'cold.bin'
  path.write_bytes(b'x' * 8192)
  samples = iter([100, 120])  # Concurrent work can increase cache during cleanup.
  monkeypatch.setattr(file_cache, '_cached_file_bytes', lambda: next(samples))
  monkeypatch.setattr(os, 'posix_fadvise', lambda *args: None)
  result = file_cache.reclaim_file_cache([path])
  assert result['advised_file_bytes'] == 8192
  assert 'bytes' not in result
  assert result['file_cache_before_bytes'] == 100
  assert result['file_cache_after_bytes'] == 120


def test_duplicate_files_are_advised_once_and_symlinks_are_not_followed(tmp_path, monkeypatch):
  source = tmp_path / 'source'
  source.mkdir()
  path = source / 'tool'
  path.write_bytes(b'x' * 4096)
  os.link(path, source / 'hardlink')
  (source / 'symlink').symlink_to(path)
  (tmp_path / 'linked-root').symlink_to(source, target_is_directory=True)
  calls = []
  monkeypatch.setattr(os, 'posix_fadvise', lambda *args: calls.append(args))
  result = file_cache.reclaim_file_cache([source, path, tmp_path / 'linked-root'])
  assert len(calls) == result['files'] == 1
  assert result['errors'] == 0


def test_file_replaced_before_open_is_not_advised(tmp_path, monkeypatch):
  path = tmp_path / 'file'
  path.write_bytes(b'x' * 4096)
  replacement = tmp_path / 'replacement'
  replacement.write_bytes(b'y' * 4096)
  real_open = os.open
  calls = []
  def replace_before_open(candidate, flags):
    replacement.replace(path)
    return real_open(candidate, flags)
  monkeypatch.setattr(os, 'open', replace_before_open)
  monkeypatch.setattr(os, 'posix_fadvise', lambda *args: calls.append(args))
  assert file_cache.reclaim_file_cache([path])['files'] == 0
  assert calls == []


def test_missing_root_and_unsupported_advice_are_optional(tmp_path, monkeypatch):
  assert file_cache.reclaim_file_cache([tmp_path / 'missing'])['files'] == 0
  monkeypatch.delattr(os, 'posix_fadvise')
  assert file_cache.reclaim_file_cache([tmp_path])['supported'] is False


def test_settled_paths_cover_managed_worktrees_without_system_walk(tmp_path):
  work = tmp_path / 'worktrees' / 'candidate'
  work.mkdir(parents=True)
  (work / '.git').write_text('gitdir: elsewhere')
  paths = file_cache.settled_turn_paths(tmp_path, 'test')
  assert work / '.git' in paths
  assert work / 'backend' in paths
  assert work / 'frontend' / 'src' in paths
  assert tmp_path / 'platform' / 'frontend' / 'node_modules' not in paths
  assert not any(str(p) in ('/usr', '/opt') for p in paths)
  assert tmp_path / 'apps' not in paths


def test_settled_cleanup_separates_tool_and_source_mapping_policy(tmp_path, monkeypatch):
  calls = []
  monkeypatch.setattr(file_cache, 'settled_tool_paths', lambda: ('tool',))
  monkeypatch.setattr(file_cache, 'settled_turn_paths', lambda *args: ('source',))
  monkeypatch.setattr(file_cache, 'reclaim_file_cache', lambda paths, **kw: calls.append((paths, kw)))
  file_cache.reclaim_settled_cache(tmp_path, 'chat')
  assert calls == [(('tool',), {'skip_mapped': False}), (('source',), {})]


def test_provider_cleanup_runs_off_event_loop_and_failure_does_not_escape(monkeypatch):
  import asyncio
  import threading
  loop_thread = threading.get_ident()
  threads = []
  monkeypatch.setattr(file_cache, 'provider_tool_paths', lambda p: ('tool',))
  def cleanup(paths, **kwargs):
    threads.append(threading.get_ident())
    assert kwargs == {'skip_mapped': False}
    raise OSError('optional advice unavailable')
  monkeypatch.setattr(file_cache, 'reclaim_file_cache', cleanup)
  asyncio.run(file_cache.reclaim_provider_cache('codex'))
  assert len(threads) == 1
  assert threads[0] != loop_thread
