from pathlib import Path

from app import frontend_assets, platform_generation


def _complete(root: Path) -> None:
  (root / "assets").mkdir(parents=True)
  (root / "index.html").write_text("ok")
  (root / "sw.js").write_text("ok")
  (root / "manifest.webmanifest").write_text("{}")


def test_pending_backend_uses_its_matching_frozen_frontend(
  tmp_path, monkeypatch,
):
  frozen = tmp_path / "frozen"
  workspace = tmp_path / "data" / "platform" / "frontend" / "dist"
  baked = tmp_path / "baked"
  for directory in (frozen, workspace, baked):
    _complete(directory)
  monkeypatch.setenv("MOBIUS_SERVED_FRONTEND_DIR", str(frozen))
  monkeypatch.setenv("MOBIUS_BAKED_STATIC_DIR", str(baked))
  monkeypatch.setattr(
    platform_generation, "generation_state", lambda: {"pending": {"id": "x"}},
  )
  frontend_assets.reset_frontend_dir_cache()

  assert frontend_assets.resolve_frontend_dir(str(tmp_path / "data")) == frozen


def test_live_frontend_publication_resumes_after_pending_clears(
  tmp_path, monkeypatch,
):
  frozen = tmp_path / "frozen"
  workspace = tmp_path / "data" / "platform" / "frontend" / "dist"
  baked = tmp_path / "baked"
  for directory in (frozen, workspace, baked):
    _complete(directory)
  monkeypatch.setenv("MOBIUS_SERVED_FRONTEND_DIR", str(frozen))
  monkeypatch.setenv("MOBIUS_BAKED_STATIC_DIR", str(baked))
  monkeypatch.setattr(
    platform_generation, "generation_state", lambda: {"pending": None},
  )
  frontend_assets.reset_frontend_dir_cache()

  assert frontend_assets.resolve_frontend_dir(str(tmp_path / "data")) == workspace


def test_successful_rollback_keeps_its_matching_frozen_frontend(
  tmp_path, monkeypatch,
):
  frozen = tmp_path / "frozen"
  workspace = tmp_path / "data" / "platform" / "frontend" / "dist"
  baked = tmp_path / "baked"
  for directory in (frozen, workspace, baked):
    _complete(directory)
  monkeypatch.setenv("MOBIUS_SERVED_FRONTEND_DIR", str(frozen))
  monkeypatch.setenv("MOBIUS_BAKED_STATIC_DIR", str(baked))
  monkeypatch.setattr(
    platform_generation,
    "generation_state",
    lambda: {
      "pending": None,
      "activation": {"status": "rolled_back_ready"},
    },
  )
  frontend_assets.reset_frontend_dir_cache()

  assert frontend_assets.resolve_frontend_dir(str(tmp_path / "data")) == frozen
