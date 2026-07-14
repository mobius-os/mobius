"""Watcher integration with per-app git commits."""

from pathlib import Path

import pytest

from app import app_git, install
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
    src, {"index.jsx": initial.encode()}, "https://x/mobius.json", "1.0.0",
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


@pytest.mark.asyncio
async def test_watcher_holds_prior_bundle_while_non_entry_conflict_unresolved(
  client, owner_token,
):
  """The invariant on the watcher side: when a merge is in progress with an
  unresolved conflict in a NON-entry file (fetch.sh), a save that touches the
  compilable index.jsx must NOT swap the bundle or commit. index.jsx compiles
  fine, so the only thing keeping the broken job-script tree from being
  finalized is this gate — without it, the app shows updated AND fetch.sh gets
  committed full of `<<<<<<<` markers (the 2026-06-08 News incident)."""
  import asyncio
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "watch-conflict"
  src.mkdir(parents=True, exist_ok=True)
  base_jsx = "export default function App(){ return <div>V0</div> }"
  base_job = "#!/bin/bash\nshared step\n"
  app_id = client.post("/api/apps/", json={
    "name": "watchconflict",
    "description": "x",
    "jsx_source": base_jsx,
    "source_dir": str(src),
  }, headers={"Authorization": f"Bearer {owner_token}"}).json()["id"]

  # Install v1 with a job script, diverge the job locally, then upstream v2
  # edits the SAME job line → a NON-entry conflict (index.jsx is unchanged).
  app_git.ensure_repo(src)
  app_git.record_upstream(
    src, {"index.jsx": base_jsx.encode(), "fetch.sh": base_job.encode()},
    "https://x/mobius.json", "1.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )
  app_git.align_local_to_upstream(src)
  (src / "fetch.sh").write_text("#!/bin/bash\nLOCAL step\n")
  app_git.commit_local(src, "local job edit")
  app_git.record_upstream(
    src,
    {"index.jsx": base_jsx.encode(), "fetch.sh": b"#!/bin/bash\nUPSTREAM step\n"},
    "https://x/mobius.json", "2.0.0",
    exec_paths=frozenset({"fetch.sh"}),
  )
  assert app_git.merge_upstream(src).status == "conflict"
  app_git.start_conflict_merge(src)
  assert (src / ".git" / "MERGE_HEAD").exists()

  head_before = app_git.head_sha(src, app_git.LOCAL_BRANCH)
  count_before = _commit_count(src)
  db = SessionLocal()
  try:
    bundle_before = (
      db.query(models.App).filter(models.App.id == app_id).first().compiled_path
    )
  finally:
    db.close()

  # The agent saves a NEW (compilable) index.jsx while the fetch.sh conflict is
  # still unresolved — the watcher fires.
  jsx_path = src / "index.jsx"
  jsx_path.write_text(
    "export default function App(){ return <div>V1</div> }", encoding="utf-8",
  )
  await _JsxHandler(asyncio.get_running_loop())._recompile(str(jsx_path))

  # No commit, no base advance — the prior version is held entirely.
  assert app_git.head_sha(src, app_git.LOCAL_BRANCH) == head_before
  assert _commit_count(src) == count_before
  assert (src / ".git" / "MERGE_HEAD").exists()
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app_id).first()
    # Bundle not swapped: still serving the prior compiled output + source.
    assert row.compiled_path == bundle_before
    assert row.jsx_source == base_jsx
  finally:
    db.close()


@pytest.mark.asyncio
async def test_resolved_conflict_keeps_old_live_state_until_full_replay(
  client, owner_token, monkeypatch,
):
  """Resolution holds old artifacts until the canonical install replay."""
  import asyncio
  import app.models as models
  from app.app_watcher import _JsxHandler, _source_dir_for_changed_path
  from app.database import SessionLocal

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "watch-resolved-update"
  src.mkdir(parents=True, exist_ok=True)
  base_jsx = "export default function App(){ return <div>V0</div> }"
  app_id = client.post("/api/apps/", json={
    "name": "watchresolved",
    "description": "x",
    "jsx_source": base_jsx,
    "source_dir": str(src),
  }, headers={"Authorization": f"Bearer {owner_token}"}).json()["id"]

  app_git.ensure_repo(src)
  app_git.record_upstream(
    src,
    {"index.jsx": base_jsx.encode(), "fetch.sh": b"base\n"},
    "https://x/mobius.json", "1.0.0",
  )
  app_git.align_local_to_upstream(src)
  (src / "fetch.sh").write_text("local\n")
  app_git.commit_local(src, "local edit")
  upstream = app_git.record_upstream(
    src,
    {"index.jsx": base_jsx.encode(), "fetch.sh": b"upstream\n"},
    "https://x/mobius.json", "2.0.0",
  )
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app_id).first()
    row.upstream_commit = upstream
    db.commit()
  finally:
    db.close()
  install.stage_pending_conflict_update(
    src,
    app_id=app_id,
    upstream_commit=upstream,
    manifest={"id": "watchresolved", "version": "2.0.0"},
    raw_base="https://example.invalid/app/",
    capability_digest="a" * 64,
    candidate_digest="b" * 64,
  )
  app_git.start_conflict_merge(src)
  assert _source_dir_for_changed_path(src / "fetch.sh") == src
  (src / "fetch.sh").write_text("local + upstream\n")

  replays = []

  async def fake_reapply(db, **kwargs):
    # The watcher must not expose any candidate artifact before the canonical
    # installer reaches its own complete commit/promotion boundary.
    assert not (src / "static" / "runtime.js").exists()
    assert kwargs["manifest"]["version"] == "2.0.0"
    assert kwargs["expected_app_id"] == app_id
    assert kwargs["expected_upstream_commit"] == upstream
    assert kwargs["expected_candidate_digest"] == "b" * 64
    replays.append(kwargs)
    row = db.query(models.App).filter(models.App.id == app_id).first()
    (src / "static").mkdir(exist_ok=True)
    (src / "static" / "runtime.js").write_bytes(
      b"export const version = 'v2'\n"
    )
    install.clear_pending_conflict_update(src)
    return row, "update", [], kwargs["manifest"], [], "clean_merge"

  monkeypatch.setattr(install, "install_from_manifest", fake_reapply)
  await _JsxHandler(asyncio.get_running_loop())._recompile(
    str(src / "fetch.sh"), force_rebuild=True,
  )

  assert len(replays) == 1
  assert not (src / ".git" / "MERGE_HEAD").exists()
  assert not (src / ".git" / install._PENDING_UPDATE_DIR).exists()
  assert (src / "static" / "runtime.js").read_bytes().endswith(b"'v2'\n")
