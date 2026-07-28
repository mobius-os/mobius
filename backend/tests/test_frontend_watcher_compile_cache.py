"""Node compile-cache retention at the Vite environment boundary."""

from pathlib import Path

import app.frontend_watcher as fw


def _cache_entry(root: Path, name: str, marker: str) -> Path:
  entry = root / "node-compile-cache" / name
  entry.mkdir(parents=True)
  (entry / "marker").write_text(marker, encoding="utf-8")
  return entry


def test_vite_env_preserves_current_compile_cache_and_prunes_stale(
  tmp_path,
  monkeypatch,
):
  cache_dir = tmp_path / ".vite-cache"
  tmp_dir = tmp_path / ".vite-tmp"
  current = _cache_entry(tmp_dir, "v24.18.0-x64-current-1000", "current")
  stale = _cache_entry(tmp_dir, "v22.23.1-x64-stale-1000", "stale")

  monkeypatch.setattr(
    fw,
    "_current_node_compile_cache_dir",
    lambda env: Path(env["TMPDIR"]) / "node-compile-cache" / current.name,
  )

  env = fw._vite_env(cache_dir, tmp_dir)

  assert env["TMPDIR"] == str(tmp_dir)
  assert (current / "marker").read_text(encoding="utf-8") == "current"
  assert not stale.exists()


def test_compile_cache_pruning_is_scoped_to_each_watcher_temp_root(
  tmp_path,
  monkeypatch,
):
  warm = tmp_path / ".vite-tmp"
  rebuild = tmp_path / ".vite-tmp-rebuild"
  warm_current = _cache_entry(warm, "v24-current", "warm current")
  warm_stale = _cache_entry(warm, "v22-stale", "warm stale")
  rebuild_stale = _cache_entry(rebuild, "v22-stale", "rebuild stale")

  monkeypatch.setattr(
    fw,
    "_current_node_compile_cache_dir",
    lambda env: Path(env["TMPDIR"]) / "node-compile-cache" / "v24-current",
  )

  fw._vite_env(tmp_path / ".vite-cache", warm)

  assert warm_current.exists()
  assert not warm_stale.exists()
  assert rebuild_stale.exists()


def test_compile_cache_pruning_handles_malformed_and_vanished_entries(
  tmp_path,
  monkeypatch,
):
  tmp_dir = tmp_path / ".vite-tmp"
  root = tmp_dir / "node-compile-cache"
  current = _cache_entry(tmp_dir, "v24-current", "current")
  malformed = root / "not-a-directory"
  malformed.write_text("junk", encoding="utf-8")
  vanished = _cache_entry(tmp_dir, "v22-vanished", "stale")
  real_rmtree = fw.shutil.rmtree

  def concurrent_rmtree(path):
    if path == vanished:
      real_rmtree(path)
      raise FileNotFoundError(path)
    real_rmtree(path)

  monkeypatch.setattr(
    fw,
    "_current_node_compile_cache_dir",
    lambda _env: current,
  )
  monkeypatch.setattr(fw.shutil, "rmtree", concurrent_rmtree)

  fw._vite_env(tmp_path / ".vite-cache", tmp_dir)

  assert current.exists()
  assert not malformed.exists()
  assert not vanished.exists()


def test_compile_cache_pruning_skips_operator_override_outside_temp_root(
  tmp_path,
  monkeypatch,
):
  tmp_dir = tmp_path / ".vite-tmp"
  stale = _cache_entry(tmp_dir, "v22-stale", "stale")
  external = tmp_path / "operator-cache" / "v24-current"

  monkeypatch.setattr(
    fw,
    "_current_node_compile_cache_dir",
    lambda _env: external,
  )

  fw._vite_env(tmp_path / ".vite-cache", tmp_dir)

  assert stale.exists()
