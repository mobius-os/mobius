"""Watcher integration with per-app git commits."""

from pathlib import Path

import pytest

from app import app_git
from app.config import get_settings


def _commit_count(repo: Path) -> int:
  return int(
    app_git._run(repo, "rev-list", "--count", app_git.LOCAL_BRANCH)
    .stdout.strip()
  )


@pytest.mark.asyncio
async def test_watcher_commits_successful_recompile_and_noops_unchanged(
  client, owner_token,
):
  """A saved JSX edit recompiles and advances local main once."""
  import asyncio
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "watch-git"
  src.mkdir(parents=True, exist_ok=True)
  initial = "export default function App(){ return <div>V0</div> }"
  app_id = client.post("/api/apps/", json={
    "name": "watchgit",
    "description": "x",
    "jsx_source": initial,
    "source_dir": str(src),
  }, headers={"Authorization": f"Bearer {owner_token}"}).json()["id"]

  app_git.ensure_repo(src)
  app_git.record_upstream(
    src, initial.encode(), "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(src)
  before = app_git.head_sha(src, app_git.LOCAL_BRANCH)
  before_count = _commit_count(src)

  jsx_path = src / "index.jsx"
  new_jsx = "export default function App(){ return <div>V1</div> }"
  jsx_path.write_text(new_jsx, encoding="utf-8")
  await _JsxHandler(asyncio.get_running_loop())._recompile(str(jsx_path))

  after = app_git.head_sha(src, app_git.LOCAL_BRANCH)
  assert after != before
  assert _commit_count(src) == before_count + 1
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app_id).first()
    assert row.jsx_source == new_jsx
  finally:
    db.close()

  await _JsxHandler(asyncio.get_running_loop())._recompile(str(jsx_path))
  assert app_git.head_sha(src, app_git.LOCAL_BRANCH) == after
  assert _commit_count(src) == before_count + 1
