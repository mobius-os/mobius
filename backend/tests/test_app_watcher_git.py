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


def test_polling_scandir_skips_data_git_and_generated_trees(tmp_path):
  """The reliable polling watcher must not rescan known-noise trees."""
  from app.app_watcher import _source_tree_scandir

  root = tmp_path / "apps"
  source = root / "notes"
  for path in (
    root / "77" / "storage",
    source / ".git" / "objects",
    source / "node_modules" / "package",
    source / "static" / "assets",
    source / "src",
  ):
    path.mkdir(parents=True)
  (source / "index.jsx").write_text("export default () => null")
  (source / "fetch.sh").write_text("#!/bin/sh\n")
  (source / "src" / "view.jsx").write_text("export const View = null")

  root_names = {entry.name for entry in _source_tree_scandir(root, str(root))}
  source_names = {
    entry.name for entry in _source_tree_scandir(root, str(source))
  }

  assert root_names == {"notes"}
  assert source_names == {"fetch.sh", "index.jsx", "src"}


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



@pytest.mark.asyncio
async def test_watcher_gate_blocks_replay_until_whole_tree_resolved(
  client, owner_token, monkeypatch, caplog,
):
  """A multi-file conflict must not replay the installer until EVERY tracked
  file is marker-free.

  When the agent has resolved the entry (index.jsx) but a sibling (fetch.sh)
  still carries `<<<<<<<` markers, the watcher event fired on the entry save
  must NOT re-enter install_from_manifest (no premature replay) and must log no
  error. Once the sibling is also resolved, the next event replays and promotes.
  """
  import asyncio
  import logging
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "watch-multi-conflict"
  src.mkdir(parents=True, exist_ok=True)
  base_jsx = "export default function App(){ return <div>V0</div> }"
  app_id = client.post("/api/apps/", json={
    "name": "watchmulti",
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
  local_jsx = "export default function App(){ return <div>LOCAL</div> }"
  (src / "index.jsx").write_text(local_jsx)
  (src / "fetch.sh").write_text("local\n")
  app_git.commit_local(src, "local edits")
  upstream_jsx = "export default function App(){ return <div>UPSTREAM</div> }"
  upstream = app_git.record_upstream(
    src,
    {"index.jsx": upstream_jsx.encode(), "fetch.sh": b"upstream\n"},
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
    manifest={"id": "watchmulti", "version": "2.0.0"},
    raw_base="https://example.invalid/app/",
    capability_digest="a" * 64,
    candidate_digest="b" * 64,
  )
  conflicts = app_git.start_conflict_merge(src)
  assert set(conflicts) == {"index.jsx", "fetch.sh"}
  assert (src / ".git" / "MERGE_HEAD").exists()
  assert app_git.has_conflict_markers(src)

  replays = []

  async def fake_reapply(db, **kwargs):
    replays.append(kwargs)
    row = db.query(models.App).filter(models.App.id == app_id).first()
    install.clear_pending_conflict_update(src)
    return row, "update", [], kwargs["manifest"], [], "clean_merge"

  monkeypatch.setattr(install, "install_from_manifest", fake_reapply)

  # Part A: resolve ONLY the entry; the sibling still carries markers.
  resolved_jsx = "export default function App(){ return <div>MERGED</div> }"
  (src / "index.jsx").write_text(resolved_jsx)
  assert app_git.has_conflict_markers(src)  # fetch.sh still unresolved

  caplog.clear()
  with caplog.at_level(logging.INFO, logger="app.app_watcher"):
    await _JsxHandler(asyncio.get_running_loop())._recompile(
      str(src / "index.jsx"), force_rebuild=True,
    )
  # No replay while a sibling is unresolved; nothing logged at error.
  assert replays == []
  assert (src / ".git" / "MERGE_HEAD").exists()
  assert (src / ".git" / install._PENDING_UPDATE_DIR).exists()
  assert not any(r.levelno >= logging.ERROR for r in caplog.records)

  # Part B: resolve the sibling too — the next event replays and promotes.
  (src / "fetch.sh").write_text("merged\n")
  assert not app_git.has_conflict_markers(src)
  caplog.clear()
  with caplog.at_level(logging.INFO, logger="app.app_watcher"):
    await _JsxHandler(asyncio.get_running_loop())._recompile(
      str(src / "fetch.sh"), force_rebuild=True,
    )
  assert len(replays) == 1
  assert not (src / ".git" / "MERGE_HEAD").exists()
  assert not (src / ".git" / install._PENDING_UPDATE_DIR).exists()
  assert not any(r.levelno >= logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_watcher_replay_conflict_mode_is_info_waitstate_not_error(
  client, owner_token, monkeypatch, caplog,
):
  """When the installer's fresh merge re-detects a conflict during replay
  (mode != "update"), the watcher treats it as a 'resolution not yet complete'
  wait-state: log at info, no rollback-as-error, no exception, no promote.

  This is the mid-flight case the old conflated post-condition raised on and the
  outer handler logged as `log.exception`. A later replay returning "update"
  promotes normally.
  """
  import asyncio
  import logging
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal

  data_dir = Path(get_settings().data_dir)
  src = data_dir / "apps" / "watch-replay-conflict"
  src.mkdir(parents=True, exist_ok=True)
  base_jsx = "export default function App(){ return <div>V0</div> }"
  app_id = client.post("/api/apps/", json={
    "name": "watchreplayconflict",
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
    bundle_before = row.compiled_path
  finally:
    db.close()
  install.stage_pending_conflict_update(
    src,
    app_id=app_id,
    upstream_commit=upstream,
    manifest={"id": "watchreplayconflict", "version": "2.0.0"},
    raw_base="https://example.invalid/app/",
    capability_digest="a" * 64,
    candidate_digest="b" * 64,
  )
  app_git.start_conflict_merge(src)
  # Agent resolves the whole tree marker-free so the pre-replay gate passes and
  # the installer replay is reached.
  (src / "fetch.sh").write_text("merged\n")
  assert not app_git.has_conflict_markers(src)

  calls = []

  async def fake_conflict(db, **kwargs):
    # The installer's own fresh three-way merge still conflicts: it commits the
    # conflict provenance, re-stages the receipt, and returns mode="conflict".
    calls.append("conflict")
    row = db.query(models.App).filter(models.App.id == app_id).first()
    return row, "conflict", [], kwargs["manifest"], ["fetch.sh"], "conflict"

  monkeypatch.setattr(install, "install_from_manifest", fake_conflict)
  caplog.clear()
  with caplog.at_level(logging.INFO, logger="app.app_watcher"):
    await _JsxHandler(asyncio.get_running_loop())._recompile(
      str(src / "fetch.sh"), force_rebuild=True,
    )
  # Wait-state: replay was attempted once, no error/exception logged, an info
  # note explains the incomplete resolution, and the bundle is NOT promoted.
  assert calls == ["conflict"]
  assert not any(r.levelno >= logging.ERROR for r in caplog.records)
  assert any(
    r.levelno == logging.INFO and "not yet complete" in r.getMessage()
    for r in caplog.records
  )
  db = SessionLocal()
  try:
    row = db.query(models.App).filter(models.App.id == app_id).first()
    assert row.compiled_path == bundle_before
  finally:
    db.close()

  # A later replay that resolves cleanly promotes (the designed self-heal). The
  # receipt is still staged and MERGE_HEAD was cleared by the first commit_local,
  # so the next event reaches the replay without an active merge.
  async def fake_update(db, **kwargs):
    calls.append("update")
    row = db.query(models.App).filter(models.App.id == app_id).first()
    install.clear_pending_conflict_update(src)
    return row, "update", [], kwargs["manifest"], [], "clean_merge"

  monkeypatch.setattr(install, "install_from_manifest", fake_update)
  caplog.clear()
  with caplog.at_level(logging.INFO, logger="app.app_watcher"):
    # Fire on the entry: after the first commit_local cleared MERGE_HEAD,
    # a non-source save (fetch.sh) no longer resolves to a rebuild, but the
    # entry always does and re-drives the still-pending install receipt.
    await _JsxHandler(asyncio.get_running_loop())._recompile(
      str(src / "index.jsx"), force_rebuild=True,
    )
  assert calls == ["conflict", "update"]
  assert not (src / ".git" / install._PENDING_UPDATE_DIR).exists()
  assert not any(r.levelno >= logging.ERROR for r in caplog.records)

