"""Reviewed update batches: server-owned, durable, and the single-update path."""

import asyncio
import json
import logging
import struct
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from fastapi import HTTPException
import pytest

from app import app_update_batches as batches
from app import fs_locks, install, models, pending_update, settled

COMMIT = "a" * 40


def request(*ids, key=None, commit=COMMIT):
  return batches.ReviewedUpdateBatch(id=key or uuid4(), updates=[{
    "app_id": app_id,
    "manifest_url": f"https://example.test/app-{app_id}/mobius.json",
    "upstream_commit": commit,
    "source_digest": "b" * 64,
    "capability_digest": "c" * 64,
  } for app_id in ids])


@pytest.fixture(autouse=True)
def lifecycle_lock(monkeypatch):
  """A lock bound to this test's loop; the module singleton outlives loops."""
  monkeypatch.setattr(fs_locks, "_lifecycle_lock", asyncio.Lock())


LANDED = "e" * 40  # an accepted source commit that contains COMMIT


class _Receipts:
  """Real pending-update receipt files, as a conflicted update writes them."""

  def __init__(self):
    self.paths = []

  def add(self, source_dir, app_id, commit=COMMIT):
    path = pending_update.pending_update_receipt_file(source_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
      "schema": 4, "app_id": app_id, "upstream_commit": commit,
      "manifest": {}, "raw_base": "", "capability_digest": "c" * 64,
      "candidate_digest": "d" * 64,
    }))
    self.paths.append(path)

  def clear(self):
    # As clear_pending_conflict_update: the file goes first, and a leftover
    # pending directory is harmless.
    for path in self.paths:
      path.unlink()
    self.paths = []


@pytest.fixture
def git_state(monkeypatch):
  """Fake commits (ancestry over a complete history); receipts are real files."""
  monkeypatch.setattr(batches.app_git, "object_ancestry",
    lambda path, reviewed, accepted: reviewed == COMMIT and accepted == LANDED)
  monkeypatch.setattr(batches.app_git, "history_is_complete", lambda *args: True)
  return _Receipts()


@pytest.fixture
def app_rows(monkeypatch):
  monkeypatch.setattr(batches, "live_app_or_404", lambda db, app_id:
    SimpleNamespace(id=app_id, name=f"App {app_id}", version="1.0"))


def add_app(db, tmp_path, name, **fields):
  source = tmp_path / name
  source.mkdir()
  app = models.App(name=name, slug=name, jsx_source="export default 1",
    source_dir=str(source), **fields)
  db.add(app)
  db.commit()
  return app.id


async def drain(owner):
  """Let accepted work finish on its own; stop() would cancel it."""
  while owner.tasks:
    await asyncio.gather(*list(owner.tasks.values()), return_exceptions=True)


@pytest.mark.asyncio
async def test_entries_run_two_at_a_time_and_keep_mixed_outcomes(tmp_path, app_rows, monkeypatch):
  entered = set()
  all_entered = asyncio.Event()
  release = asyncio.Event()

  async def execute(update):
    app_id = update["app_id"]
    entered.add(app_id)
    if len(entered) == batches._RUNNING_ENTRIES:
      all_entered.set()
    await release.wait()
    if app_id == 3:
      raise HTTPException(409, detail={"code": "update_changed", "message": "Review the changed candidate."})
    if app_id == 4:
      raise RuntimeError("test failure")
    if app_id == 5:
      raise HTTPException(409, detail={
        "code": "git_transfer_timeout", "message": "Timed out.", "retryable": True,
      })
    return {"status": "updated" if app_id == 1 else "conflict", "version": "2.0"}

  published = []
  monkeypatch.setattr(batches, "get_system_broadcast",
    lambda: SimpleNamespace(publish=published.append))
  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  body = request(1, 2, 3, 4, 5)
  first = owner.submit(body, None)
  await asyncio.wait_for(all_entered.wait(), timeout=2)
  assert first["phase"] == "applying"
  # Only the transfer slots run; the rest are really queued.
  await asyncio.sleep(0.01)
  assert [e["status"] for e in owner.snapshot(first["id"])["entries"]] == [
    "applying", "applying", "queued", "queued", "queued",
  ]
  assert entered == {1, 2}
  assert len(owner.tasks) == 1
  # The same id is the same approval: a retry attaches, never starts twice.
  assert owner.submit(body, None)["id"] == first["id"]
  release.set()
  await drain(owner)
  final = owner.snapshot(first["id"])
  assert [e["status"] for e in final["entries"]] == ["updated", "conflict", "failed", "failed", "failed"]
  assert [(e["error"], e["error_code"], e["retryable"]) for e in final["entries"][2:]] == [
    ("Review the changed candidate.", "update_changed", False),
    ("This update could not finish. Check it again before retrying.", None, False),
    ("Timed out.", "git_transfer_timeout", True),
  ]
  assert final["entries"][0]["error_code"] is None
  # Like the single route: only durable changes refresh app lists.
  assert sorted(event["appId"] for event in published) == ["1", "2"]
  assert final["phase"] == "complete"
  assert "updates" not in final
  assert json.loads((owner.root / f"{first['id']}.json").read_text())["phase"] == "complete"
  assert (owner.root / f"{first['id']}.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_entries_share_the_single_update_path_and_lifecycle_lock(tmp_path, monkeypatch, db):
  """Transfers overlap outside every lock; local installs take the lock in turn."""
  ids = []
  for name in ("first", "second"):
    app_id = add_app(db, tmp_path, name, version="1.0")
    subprocess.run(["git", "init", "-q", str(tmp_path / name)], check=True)
    ids.append(app_id)
  fetching, installing = set(), []
  both_fetching, release_fetch = asyncio.Event(), asyncio.Event()
  overlap = []

  async def fetch_app_commit(source_dir, ref, **_):
    assert not fs_locks.install_uninstall_lock().locked()
    fetching.add(source_dir)
    if len(fetching) == 2:
      both_fetching.set()
    await release_fetch.wait()
    return ref

  async def install_from_manifest(session, **kwargs):
    assert fs_locks.install_uninstall_lock().locked()
    installing.append(kwargs["reviewed_app_id"])
    overlap.append(len(installing))
    await asyncio.sleep(0.01)
    installing.remove(kwargs["reviewed_app_id"])
    assert kwargs["reviewed_upstream_commit"] == COMMIT
    assert kwargs["reviewed_source_digest"] == "b" * 64
    assert kwargs["reviewed_capability_digest"] == "c" * 64
    assert kwargs["prepared"] is None
    return SimpleNamespace(mode="update", app=SimpleNamespace(version="2.0"), warnings=["w"])

  monkeypatch.setattr(install, "fetch_app_commit", fetch_app_commit)
  monkeypatch.setattr(install, "install_from_manifest", install_from_manifest)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  accepted = owner.submit(request(*ids), db)
  await asyncio.wait_for(both_fetching.wait(), timeout=2)
  release_fetch.set()
  await drain(owner)
  final = owner.snapshot(accepted["id"])
  assert [(e["status"], e["version"], e["warnings"]) for e in final["entries"]] == [
    ("updated", "2.0", ["w"]), ("updated", "2.0", ["w"]),
  ]
  assert max(overlap) == 1


@pytest.mark.asyncio
async def test_unrelated_batch_never_hides_actionable_results(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  keys = [str(uuid4()) for _ in range(4)]
  for index, (app_id, status) in enumerate([(1, "conflict"), (2, "failed"), (3, "updated")]):
    owner._save({"id": keys[index], "created_at": f"2026-01-0{index + 1}",
      "updates": [], "entries": [{"app_id": app_id, "status": status}]})
  assert [r["id"] for r in owner.recent()] == keys[2::-1]
  owner._save({"id": keys[3], "created_at": "2026-01-04", "updates": [],
    "entries": [{"app_id": 1, "status": "updated"}]})
  assert [r["id"] for r in owner.recent()] == [keys[3], keys[1]]
  # Older receipts remain addressable for exact-id retries and audit.
  assert owner.snapshot(keys[0])["entries"][0]["status"] == "conflict"


@pytest.mark.asyncio
async def test_closing_progress_does_not_cancel_approved_work(tmp_path, app_rows):
  entered, release = asyncio.Event(), asyncio.Event()

  async def execute(update):
    entered.set()
    await release.wait()
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  accepted = owner.submit(request(1), None)
  stream = owner.snapshots(accepted["id"])
  assert (await anext(stream))["id"] == accepted["id"]
  await entered.wait()
  await stream.aclose()
  assert not owner.listeners
  assert owner.recent()[0]["phase"] == "applying"
  release.set()
  await drain(owner)
  assert owner.snapshot(accepted["id"])["entries"][0]["status"] == "updated"


@pytest.mark.asyncio
async def test_shutdown_settles_running_installs_and_records_what_landed(tmp_path, db, git_state, monkeypatch):
  """Cancellation waits for the install pass, then reads the durable row."""
  from app.runtime_supervisors import RuntimeSupervisors

  landed = add_app(db, tmp_path, "landed", version="1.0", upstream_commit="0" * 40)
  rolled_back = add_app(db, tmp_path, "rolled-back", version="1.0", upstream_commit="0" * 40)
  queued = add_app(db, tmp_path, "queued", version="1.0", upstream_commit="0" * 40)
  monkeypatch.setattr(batches, "_RUNNING_ENTRIES", 2)
  entered, release = asyncio.Event(), asyncio.Event()
  started = set()
  order = []

  async def pass_in_thread(app_id):
    # Stands in for a locked install pass whose threads outlive a cancel.
    await release.wait()
    if app_id == landed:
      with batches.SessionLocal() as session:
        row = session.get(models.App, app_id)
        row.upstream_commit, row.source_commit, row.version = COMMIT, LANDED, "2.0"
        session.commit()
    order.append(f"settled {app_id}")
    return {"status": "updated"}

  async def execute(update):
    started.add(update["app_id"])
    if len(started) == 2:
      entered.set()
    return await settled.settle(pass_in_thread(update["app_id"]))

  async def resident():
    try:
      await asyncio.Event().wait()
    finally:
      order.append("other owner stopped")

  runtime = RuntimeSupervisors(settings=SimpleNamespace(data_dir=str(tmp_path)),
    logger=logging.getLogger(__name__), restart_authorization=None,
    restart_fallback_chats=[])
  runtime.app_updates.execute = execute
  await runtime.app_updates.start()
  runtime._spawn("test-resident", resident())
  accepted = runtime.app_updates.submit(request(landed, rolled_back, queued), db)
  await entered.wait()
  stopping = asyncio.create_task(runtime.stop())
  await asyncio.sleep(0.05)
  assert not stopping.done()
  assert not runtime.app_updates.accepting
  assert order == []
  release.set()
  await stopping
  assert order[-1] == "other owner stopped"
  entries = runtime.app_updates.snapshot(accepted["id"])["entries"]
  assert [(e["status"], e["version"]) for e in entries] == [
    ("updated", "2.0"), ("failed", "1.0"), ("failed", "1.0"),
  ]
  assert entries[1]["error"] == entries[2]["error"] == batches._NOT_APPLIED
  assert started == {landed, rolled_back}  # the queued entry never ran
  saved = json.loads((runtime.app_updates.root / f"{accepted['id']}.json").read_text())
  assert saved["phase"] == "complete"
  with pytest.raises(HTTPException) as closed:
    runtime.app_updates.submit(request(landed), db)
  assert closed.value.status_code == 503


@pytest.mark.asyncio
async def test_batch_identity_and_running_app_cannot_be_repurposed(tmp_path, app_rows):
  release = asyncio.Event()

  async def execute(update):
    await release.wait()
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  body = request(1)
  owner.submit(body, None)
  try:
    with pytest.raises(HTTPException, match="different reviewed"):
      owner.submit(request(2, key=body.id), None)
    with pytest.raises(HTTPException) as conflict:
      owner.submit(request(1), None)
    assert conflict.value.detail["code"] == "app_update_running"
    assert conflict.value.detail["batch_id"] == str(body.id)
  finally:
    release.set()
    await drain(owner)


@pytest.mark.asyncio
async def test_restart_settles_unfinished_entries_from_durable_rows(tmp_path, db, git_state):
  """Boot never replays an approval; the served source says what happened."""
  landed = add_app(db, tmp_path, "landed", version="2.0", upstream_commit=COMMIT,
    source_commit=LANDED)
  conflicted = add_app(db, tmp_path, "conflicted", version="1.0", upstream_commit=COMMIT,
    source_commit="1" * 40)
  git_state.add(tmp_path / "conflicted", conflicted)
  # A conflicted retry records the reviewed upstream; clearing its receipt
  # never serves it. Upstream alone must not read as updated.
  cleared = add_app(db, tmp_path, "cleared", version="1.0", upstream_commit=COMMIT,
    source_commit="1" * 40)
  removed = add_app(db, tmp_path, "removed", version="1.0", upstream_commit=COMMIT,
    deleted_at=datetime.now(UTC).replace(tzinfo=None))
  finished = add_app(db, tmp_path, "finished", version="2.0", upstream_commit=COMMIT)
  body = request(finished, landed, conflicted, cleared, removed)
  key = str(body.id)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  owner._save({"id": key, "created_at": _now(),
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": finished, "status": "updated", "version": "2.0", "error": ""}] + [
      {"app_id": app_id, "status": status, "version": "1.0", "error": ""}
      for app_id, status in [(landed, "applying"), (conflicted, "applying"),
                             (cleared, "applying"), (removed, "queued")]
    ]})

  async def forbidden(update):
    pytest.fail("Boot must not replay an uncertain operation.")

  reboot = batches.AppUpdateBatches(str(tmp_path), execute=forbidden)
  await reboot.start()
  assert reboot.accepting
  result = reboot.submit(body, db)
  assert [(e["status"], e["version"]) for e in result["entries"]] == [
    ("updated", "2.0"), ("updated", "2.0"), ("conflict", "1.0"),
    ("failed", "1.0"), ("failed", "1.0"),
  ]
  assert result["entries"][3]["error"] == batches._NOT_APPLIED
  assert result["entries"][3]["error_code"] == "update_not_applied"
  assert result["entries"][4]["error"] == "This app is no longer installed."
  assert result["phase"] == "complete"
  assert reboot.tasks == {}
  assert json.loads((reboot.root / f"{key}.json").read_text())["phase"] == "complete"


def _now():
  return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_restart_prunes_only_old_finished_receipts(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
  expired, recent_key = str(uuid4()), str(uuid4())
  for key in (expired, recent_key):
    owner._save({"id": key, "created_at": old, "updates": [],
      "entries": [{"app_id": 1, "status": "updated"}]})
  record = json.loads((owner.root / f"{expired}.json").read_text())
  record["updated_at"] = old
  (owner.root / f"{expired}.json").write_text(json.dumps(record))
  reboot = batches.AppUpdateBatches(str(tmp_path))
  await reboot.start()
  assert set(reboot.records) == {recent_key}
  assert not (reboot.root / f"{expired}.json").exists()


@pytest.mark.asyncio
async def test_receipt_failure_never_starts_unrecorded_work(tmp_path, app_rows, monkeypatch):
  calls = []

  async def execute(update):
    calls.append(update)
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  def no_space(*args, **kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(batches, "atomic_write", no_space)
  with pytest.raises(HTTPException) as unavailable:
    owner.submit(request(1), None)
  assert unavailable.value.status_code == 503
  assert "could not be saved" in unavailable.value.detail
  assert calls == []
  assert owner.tasks == {}
  assert not owner.accepting


@pytest.mark.asyncio
async def test_damaged_progress_disables_only_updates_without_discarding_evidence(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  path = owner.root / f"{uuid4()}.json"
  path.write_text("interrupted bytes")
  await owner.start()
  assert not owner.accepting
  assert path.read_text() == "interrupted bytes"
  with pytest.raises(HTTPException, match="could not be read"):
    owner.submit(request(1), None)


def test_duplicate_apps_and_incomplete_review_are_rejected():
  with pytest.raises(ValueError, match="only once"):
    request(1, 1)
  body = request(1).model_dump()
  del body["updates"][0]["source_digest"]
  with pytest.raises(ValueError, match="source_digest"):
    batches.ReviewedUpdateBatch.model_validate(body)


@pytest.mark.asyncio
async def test_result_write_failure_lets_started_siblings_finish_and_closes_admission(tmp_path, app_rows, monkeypatch):
  entered, release = asyncio.Event(), asyncio.Event()
  started = set()
  finished = set()

  async def execute(update):
    app_id = update["app_id"]
    started.add(app_id)
    if len(started) == 2:
      entered.set()
    await release.wait()
    finished.add(app_id)
    return {"status": "updated"}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  accepted = owner.submit(request(1, 2), None)
  await entered.wait()

  def no_space(*args, **kwargs):
    raise OSError("disk full")

  monkeypatch.setattr(batches, "atomic_write", no_space)
  release.set()
  await drain(owner)
  assert finished == {1, 2}
  assert not owner.accepting
  with pytest.raises(HTTPException, match="could not be saved"):
    owner.snapshot(accepted["id"])
  with pytest.raises(HTTPException):
    owner.submit(request(3), None)


def test_batch_routes_require_manage_apps_and_owner_control(client, owner_token, monkeypatch):
  owner = SimpleNamespace(recent=lambda: [])
  async def batches_owner(request):
    return owner
  monkeypatch.setattr("app.routes.app_update_batches.batches", batches_owner)
  assert client.get("/api/apps/update-batches").status_code == 401
  response = client.get("/api/apps/update-batches", headers={"Authorization": f"Bearer {owner_token}"})
  assert response.status_code == 200
  assert response.json() == {"batches": []}


def test_batch_routes_report_unavailable_owner(client, owner_token):
  headers = {"Authorization": f"Bearer {owner_token}"}
  response = client.get("/api/apps/update-batches", headers=headers)
  assert response.status_code == 503


def test_unprivileged_app_and_delegated_agent_cannot_admit_batches(client, owner_token, db, tmp_path):
  from app import auth

  owner = db.query(models.Owner).one()
  app = models.App(name="Ordinary app", slug="ordinary-batch-test", jsx_source="export default 1", source_dir=str(tmp_path))
  db.add(app)
  db.commit()
  token = auth.create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce)
  app_headers = {"Authorization": f"Bearer {token}"}
  body = request(app.id).model_dump(mode="json")
  assert client.get("/api/apps/update-batches", headers=app_headers).status_code == 403
  assert client.post("/api/apps/update-batches", headers=app_headers, json=body).status_code == 403

  delegated = auth.create_agent_token(
    "batch-child", owner.username, owner.token_epoch,
    delegation_id="batch-delegation", delegation_chat="batch-child",
  )
  assert client.post("/api/apps/update-batches", headers={
    "Authorization": f"Bearer {delegated}",
  }, json=body).status_code == 403


def test_store_permission_admits_exact_review_but_never_incomplete_consent(client, owner_token, db, tmp_path, monkeypatch):
  from app import auth

  owner = db.query(models.Owner).one()
  store = models.App(name="Store", slug="store-batch-test", manage_apps=True,
    jsx_source="export default 1", source_dir=str(tmp_path))
  db.add(store)
  db.commit()
  token = auth.create_app_token(store.id, owner.username, owner.token_epoch, store.token_nonce)
  headers = {"Authorization": f"Bearer {token}"}
  admitted = []

  def submit(body, session):
    admitted.append(body.model_dump(mode="json"))
    return {"id": str(body.id), "phase": "applying"}

  async def batches_owner(request):
    return SimpleNamespace(submit=submit)
  monkeypatch.setattr("app.routes.app_update_batches.batches", batches_owner)
  body = request(store.id).model_dump(mode="json")
  response = client.post("/api/apps/update-batches", headers=headers, json=body)
  assert response.status_code == 200, response.text
  assert admitted == [body]
  del body["updates"][0]["upstream_commit"]
  assert client.post("/api/apps/update-batches", headers=headers, json=body).status_code == 422
  assert len(admitted) == 1


@pytest.mark.asyncio
async def test_progress_stream_coalesces_to_latest_result(tmp_path, app_rows):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = str(uuid4())
  record = {"id": key, "created_at": "2026-01-01", "updates": [],
            "entries": [{"app_id": 1, "status": "queued"}]}
  owner._save(record)
  stream = owner.snapshots(key)
  assert (await anext(stream))["entries"][0]["status"] == "queued"
  record["entries"][0]["status"] = "applying"
  owner._save(record)
  record["entries"][0]["status"] = "updated"
  owner._save(record)
  assert (await anext(stream))["entries"][0]["status"] == "updated"
  with pytest.raises(StopAsyncIteration):
    await anext(stream)
  assert not owner.listeners


@pytest.mark.asyncio
async def test_conflicted_entry_follows_its_resolver_until_the_answer_is_applied(tmp_path, db, git_state):
  app_id = add_app(db, tmp_path, "resolved", version="1.0", source_commit="d" * 40,
    upstream_commit=COMMIT)
  git_state.add(tmp_path / "resolved", app_id)

  async def execute(update):
    return {"status": "conflict", "version": "1.0", "warnings": []}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  accepted = owner.submit(request(app_id), db)
  await drain(owner)
  key = accepted["id"]
  assert owner.snapshot(key)["entries"][0]["status"] == "conflict"
  assert owner.snapshot(key)["entries"][0]["resolver_chat_id"] is None

  # The owner opens a resolver for this exact upstream commit.
  row = db.get(models.App, app_id)
  row.conflict_resolver_chat_id = "resolver-chat"
  row.conflict_resolver_upstream_commit = COMMIT
  db.commit()
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["resolver_chat_id"]) == ("conflict", "resolver-chat")

  # Only the applied answer, with its pending receipt gone, finishes the entry.
  row = db.get(models.App, app_id)
  row.version, row.source_commit = "2.0", LANDED
  db.commit()
  git_state.clear()
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["version"], entry["resolver_chat_id"]) == ("updated", "2.0", None)


@pytest.mark.asyncio
async def test_conflict_cleared_without_its_answer_is_no_longer_actionable(tmp_path, db, git_state):
  """resolve-update can drop a stale receipt; the update then did not land."""
  app_id = add_app(db, tmp_path, "dropped", version="1.0", source_commit="d" * 40,
    upstream_commit=COMMIT, conflict_resolver_chat_id="old-resolver",
    conflict_resolver_upstream_commit=COMMIT)
  git_state.add(tmp_path / "dropped", app_id)

  async def execute(update):
    return {"status": "conflict", "version": "1.0", "warnings": []}

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  key = owner.submit(request(app_id), db)["id"]
  await drain(owner)
  await owner.refresh_results(key, db)
  assert owner.snapshot(key)["entries"][0]["resolver_chat_id"] == "old-resolver"

  git_state.clear()  # cleared, but the served source never took the commit
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["error_code"], entry["resolver_chat_id"]) == (
    "failed", "update_not_applied", None,
  )


@pytest.mark.asyncio
async def test_unreadable_progress_is_retried_on_the_next_request(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  path = owner.root / f"{uuid4()}.json"
  path.write_text("interrupted bytes")
  await owner.start()
  assert not owner.accepting
  await owner.retry_start()
  assert not owner.accepting and "retried on the next request" in owner.failure
  path.unlink()  # the owner repaired or removed the evidence
  await owner.retry_start()
  assert owner.accepting and owner.failure is None


@pytest.mark.asyncio
async def test_a_cancelled_retry_keeps_the_failure_so_the_next_request_retries(
  tmp_path, monkeypatch,
):
  from tests.cancel_helpers import BlockedWorker

  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  path = owner.root / f"{uuid4()}.json"
  path.write_text("interrupted bytes")
  await owner.start()
  failure = owner.failure
  path.unlink()
  worker = BlockedWorker(wraps=owner._load_receipts)
  monkeypatch.setattr(owner, "_load_receipts", worker)
  retry = asyncio.create_task(owner.retry_start())
  assert await asyncio.to_thread(worker.entered.wait, 5)
  retry.cancel()
  await asyncio.sleep(.02)
  assert not retry.done()  # The read finishes before the retry unwinds.
  worker.release.set()
  with pytest.raises(asyncio.CancelledError):
    await retry
  assert (owner.failure, owner._read_failed, owner.accepting) == (failure, True, False)
  monkeypatch.undo()
  await owner.retry_start()
  assert owner.accepting and owner.failure is None


@pytest.mark.asyncio
async def test_a_finished_batch_keeps_a_save_lock_a_refresh_still_holds(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = str(uuid4())
  lock = owner._save_locks[key] = asyncio.Lock()
  await lock.acquire()
  done = asyncio.get_running_loop().create_future()
  done.set_result(None)
  owner._finished(key, done)
  # Dropping it would let the next save run beside the refresh's write.
  assert owner._save_locks[key] is lock
  lock.release()


@pytest.mark.asyncio
async def test_single_update_transfers_before_the_lock_and_cancels_safely(tmp_path, db, monkeypatch):
  """The shared path: no Git transfer under the lock; a cancel waits for the pass."""
  app_id = add_app(db, tmp_path, "single", version="1.0")
  subprocess.run(["git", "init", "-q", str(tmp_path / "single")], check=True)
  events = []
  in_pass, release = asyncio.Event(), asyncio.Event()

  async def fetch_app_commit(source_dir, ref, **_):
    events.append(("fetch", fs_locks.install_uninstall_lock().locked()))
    return ref

  async def locked_pass():
    in_pass.set()
    await release.wait()
    events.append(("pass settled", fs_locks.install_uninstall_lock().locked()))

  async def install_from_manifest(session, **kwargs):
    return await settled.settle(locked_pass())

  monkeypatch.setattr(install, "fetch_app_commit", fetch_app_commit)
  monkeypatch.setattr(install, "install_from_manifest", install_from_manifest)
  update = request(app_id).updates[0].model_dump()
  task = asyncio.create_task(batches.install_reviewed_update(update))
  await in_pass.wait()
  task.cancel()
  await asyncio.sleep(0.05)
  assert not task.done()
  assert fs_locks.install_uninstall_lock().locked()
  release.set()
  with pytest.raises(asyncio.CancelledError):
    await task
  assert events == [("fetch", False), ("pass settled", True)]
  assert not fs_locks.install_uninstall_lock().locked()


@pytest.mark.asyncio
async def test_database_error_at_boot_disables_only_batches(tmp_path, monkeypatch):
  """Settling old batches can fail; readiness for everything else must hold."""
  from sqlalchemy.exc import OperationalError

  from app import runtime_supervisors
  from app.runtime_supervisors import RuntimeSupervisors

  runtime = RuntimeSupervisors(settings=SimpleNamespace(data_dir=str(tmp_path)),
    logger=logging.getLogger(__name__), restart_authorization=None,
    restart_fallback_chats=[])
  body = request(1)
  key = str(body.id)
  runtime.app_updates.root.mkdir(parents=True)
  (runtime.app_updates.root / f"{key}.json").write_text(json.dumps({
    "id": key, "created_at": _now(), "updated_at": _now(), "phase": "applying",
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": 1, "status": "applying"}],
  }))

  def database_down(reviewed):
    raise OperationalError("SELECT", {}, Exception("database is locked"))

  async def required_owners():
    for name in runtime_supervisors.REQUIRED_DATABASE_SUPERVISORS:
      runtime._spawn(name, asyncio.Event().wait())

  monkeypatch.setattr(batches, "_settled_outcomes", database_down)
  monkeypatch.setattr(runtime, "_start_chat_supervisors", required_owners)
  try:
    await runtime.start_database_services()
    assert runtime.database_service_readiness()[0] is True
    assert not runtime.app_updates.accepting
    with pytest.raises(HTTPException) as unavailable:
      runtime.app_updates.submit(request(2), None)
    assert unavailable.value.status_code == 503
    assert "could not be read" in unavailable.value.detail
    # The receipt is evidence, never rewritten by a failed settlement.
    assert json.loads((runtime.app_updates.root / f"{key}.json").read_text())["phase"] == "applying"
  finally:
    await runtime.stop()


def test_progress_stream_route_sends_snapshots_and_ends_when_complete(
  client, owner_token, monkeypatch, tmp_path,
):
  from app.main import app as application

  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.accepting = True
  owner.root.mkdir(parents=True)
  key = str(uuid4())
  owner._save({"id": key, "created_at": _now(), "updates": [],
    "entries": [{"app_id": 1, "status": "updated", "name": "One"}]})
  monkeypatch.setattr(application.state, "runtime_supervisors",
    SimpleNamespace(app_updates=owner), raising=False)
  headers = {"Authorization": f"Bearer {owner_token}"}
  with client.stream("GET", f"/api/apps/update-batches/{key}/events", headers=headers) as response:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line for line in response.iter_lines() if line]
  assert len(frames) == 1 and frames[0].startswith("data: ")
  snapshot = json.loads(frames[0][len("data: "):])
  assert (snapshot["id"], snapshot["phase"], snapshot["entries"][0]["status"]) == (
    key, "complete", "updated",
  )
  assert not owner.listeners
  assert client.get(f"/api/apps/update-batches/{uuid4()}/events", headers=headers).status_code == 404
  listed = client.get("/api/apps/update-batches", headers=headers)
  assert [b["id"] for b in listed.json()["batches"]] == [key]


def test_landed_rule_against_a_real_repository(tmp_path):
  repo = tmp_path / "repo"
  subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

  def commit(message):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
      "commit", "-q", "--allow-empty", "-m", message], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
      capture_output=True, text=True, check=True).stdout.strip()

  base = commit("base")
  reviewed = commit("reviewed upstream")
  resolved = commit("resolver answer")
  subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "other", base], check=True)
  unrelated = commit("served without the update")

  assert batches._contains(str(repo), reviewed, resolved) is True
  assert batches._contains(str(repo), reviewed, reviewed) is True
  assert batches._contains(str(repo), reviewed, unrelated) is False
  assert batches._contains(str(repo), reviewed, base) is False
  row = (str(repo), resolved, "1.0", None, None)
  assert batches._settled_outcome(1, reviewed, row)["status"] == "updated"
  # A receipt naming the reviewed commit means its answer is not installed.
  _Receipts().add(repo, 1, reviewed)
  assert batches._settled_outcome(1, reviewed, row)["status"] == "conflict"


def test_landed_update_with_a_leftover_pending_directory_reads_updated(tmp_path, db):
  """A leftover pending directory, or a later update's receipt, is not this one's."""
  repo = tmp_path / "leftover"
  subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)

  def commit(message):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
      "commit", "-q", "--allow-empty", "-m", message], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
      capture_output=True, text=True, check=True).stdout.strip()

  reviewed = commit("reviewed upstream")
  served = commit("resolved answer")
  app = models.App(name="Leftover", slug="leftover", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, upstream_commit=reviewed, version="2.0")
  db.add(app)
  db.commit()
  receipt = pending_update.pending_update_receipt_file(repo)
  receipt.parent.mkdir(parents=True)
  (receipt.parent / "checkout").mkdir()  # the checkout not yet removed
  assert pending_update.pending_conflict_update_receipt_present(repo)
  outcome = batches._settled_outcomes({app.id: reviewed})[app.id]
  assert (outcome["status"], outcome["version"]) == ("updated", "2.0")
  # A later update's receipt does not unseat the one that landed.
  later = commit("later upstream")
  _Receipts().add(repo, app.id, later)
  outcome = batches._settled_outcomes({app.id: reviewed})[app.id]
  assert (outcome["status"], outcome["version"]) == ("updated", "2.0")
  # A receipt naming the reviewed commit still means pending: a conflict.
  _Receipts().add(repo, app.id, reviewed)
  assert batches._settled_outcomes({app.id: reviewed})[app.id]["status"] == "conflict"


@pytest.mark.asyncio
async def test_failure_clears_only_once_the_reviewed_commit_lands(tmp_path, db, git_state):
  app_id = add_app(db, tmp_path, "later", version="1.0", source_commit="1" * 40)

  async def execute(update):
    raise HTTPException(409, detail={"code": "update_changed", "message": "Changed."})

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  key = owner.submit(request(app_id), db)["id"]
  await drain(owner)
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  # Not landed: the failure keeps its own reason.
  assert (entry["status"], entry["error_code"]) == ("failed", "update_changed")

  row = db.get(models.App, app_id)
  row.source_commit, row.version = LANDED, "2.0"  # updated one at a time
  db.commit()
  await owner.refresh_results(key, db)
  entry = owner.snapshot(key)["entries"][0]
  assert (entry["status"], entry["version"], entry["error_code"]) == ("updated", "2.0", None)


@pytest.mark.asyncio
async def test_concurrent_retries_after_a_failed_boot_read_load_once(tmp_path, monkeypatch):
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  bad = owner.root / f"{uuid4()}.json"
  bad.write_text("interrupted bytes")
  await owner.start()
  bad.unlink()
  loads = []
  real = owner._load_receipts
  def counted():
    loads.append(1)
    return real()
  monkeypatch.setattr(owner, "_load_receipts", counted)
  await asyncio.gather(*(owner.retry_start() for _ in range(5)))
  assert loads == [1]
  assert owner.accepting


@pytest.mark.asyncio
async def test_settlement_write_failure_at_boot_is_not_retried(tmp_path, monkeypatch, app_rows):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  body = request(1)
  key = str(body.id)
  owner._save({"id": key, "created_at": _now(),
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": 1, "status": "applying"}]})
  monkeypatch.setattr(batches, "_settled_outcomes",
    lambda reviewed: {1: {"status": "failed", "error": batches._NOT_APPLIED}})
  def no_space(*args, **kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(batches, "atomic_write", no_space)
  reboot = batches.AppUpdateBatches(str(tmp_path))
  await reboot.start()
  assert reboot.failure == batches._UNSAVED
  loads = []
  monkeypatch.setattr(reboot, "_load_receipts", lambda: loads.append(1))
  await reboot.retry_start()
  assert loads == [] and not reboot.accepting


@pytest.mark.asyncio
async def test_progress_stream_ends_cleanly_when_saving_fails(tmp_path, app_rows, monkeypatch):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = str(uuid4())
  record = {"id": key, "created_at": _now(), "updates": [],
            "entries": [{"app_id": 1, "status": "applying"}]}
  owner._save(record)
  stream = owner.snapshots(key)
  assert (await anext(stream))["phase"] == "applying"
  def no_space(*args, **kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(batches, "atomic_write", no_space)
  with pytest.raises(OSError):
    owner._save(record)
  with pytest.raises(StopAsyncIteration):
    await anext(stream)
  assert not owner.listeners


@pytest.mark.asyncio
async def test_shutdown_wait_is_bounded(tmp_path, app_rows, monkeypatch):
  monkeypatch.setattr(batches, "_STOP_GRACE_SECONDS", 0.05)
  entered, release = asyncio.Event(), asyncio.Event()

  async def stuck_pass():
    await release.wait()
    return {"status": "updated"}

  async def execute(update):
    entered.set()
    return await settled.settle(stuck_pass())

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  owner.submit(request(1), None)
  await entered.wait()
  await asyncio.wait_for(owner.stop(), timeout=2)
  assert owner.tasks  # still settling; boot would settle it after a kill
  release.set()
  monkeypatch.setattr(batches, "_settled_outcomes",
    lambda reviewed: {1: {"status": "updated"}})
  await drain(owner)


def test_guest_progress_stream_does_not_lose_its_principal(client, owner_token, monkeypatch, tmp_path):
  """A shared-browser guest's stream copies the owner before the session closes."""
  from app.config import get_settings
  from app.main import app as application
  from app.routes.browser_access import _limiter
  from tests.test_browser_access_routes import invite

  _limiter.reset()
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  _grant, token, _secret = invite(client)
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.accepting = True
  owner.root.mkdir(parents=True)
  key = str(uuid4())
  owner._save({"id": key, "created_at": _now(), "updates": [],
    "entries": [{"app_id": 1, "status": "updated", "name": "One"}]})
  monkeypatch.setattr(application.state, "runtime_supervisors",
    SimpleNamespace(app_updates=owner), raising=False)
  guest = {"Authorization": f"Bearer {token}"}
  assert client.get(f"/api/apps/update-batches/{key}", headers=guest).status_code == 200
  with client.stream("GET", f"/api/apps/update-batches/{key}/events", headers=guest) as response:
    assert response.status_code == 200
    frames = [line for line in response.iter_lines() if line]
  assert len(frames) == 1 and json.loads(frames[0][len("data: "):])["id"] == key


@pytest.mark.asyncio
async def test_refresh_save_failure_reports_the_pause(tmp_path, db, git_state, monkeypatch):
  app_id = add_app(db, tmp_path, "pause", version="1.0", source_commit=LANDED)

  async def execute(update):
    raise HTTPException(409, detail={"code": "update_changed", "message": "Changed."})

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  key = owner.submit(request(app_id), db)["id"]
  await drain(owner)
  def no_space(*args, **kwargs):
    raise OSError("disk full")
  monkeypatch.setattr(batches, "atomic_write", no_space)
  with pytest.raises(HTTPException) as paused:
    await owner.refresh_results(key, db)  # the update landed; saving fails
  assert paused.value.status_code == 503
  assert paused.value.detail == batches._UNSAVED


@pytest.mark.asyncio
async def test_off_loop_saves_keep_the_latest_progress(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = str(uuid4())
  record = {"id": key, "created_at": _now(), "updates": [],
            "entries": [{"app_id": 1, "status": "applying"}]}
  owner._save(record)

  async def change(status):
    record["entries"][0]["status"] = status
    await owner._save_off_loop(record)

  await asyncio.gather(change("applying"), change("failed"), change("updated"))
  saved = json.loads((owner.root / f"{key}.json").read_text())
  assert saved["entries"][0]["status"] == record["entries"][0]["status"] == "updated"


@pytest.mark.asyncio
async def test_boot_removes_temporary_files_a_crash_left(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  leftover = owner.root / f".{uuid4()}.json.abc123.tmp"
  leftover.write_text('{"partial')
  await owner.start()
  assert owner.accepting
  assert not leftover.exists()


@pytest.mark.asyncio
async def test_runtime_prunes_finished_batches_past_retention(tmp_path, app_rows):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
  key = str(uuid4())
  owner._save({"id": key, "created_at": old, "updates": [],
    "entries": [{"app_id": 9, "status": "updated"}]})
  owner.records[key]["updated_at"] = old
  owner._save_locks[key] = asyncio.Lock()

  async def execute(update):
    return {"status": "updated"}

  owner.execute = execute
  fresh = owner.submit(request(1), None)["id"]
  await drain(owner)
  assert key not in owner.records and key not in owner._save_locks
  assert not (owner.root / f"{key}.json").exists()
  assert fresh in owner.records and fresh in owner._save_locks  # Kept until pruning.


@pytest.mark.asyncio
async def test_a_retry_settles_receipts_off_the_loop_and_applies_them_on_it(
  tmp_path, monkeypatch,
):
  """A retry runs beside live requests: no record or save changes off the loop."""
  import threading

  owner = batches.AppUpdateBatches(str(tmp_path))
  owner.root.mkdir()
  bad = owner.root / f"{uuid4()}.json"
  bad.write_text("interrupted bytes")
  await owner.start()
  assert not owner.accepting
  bad.unlink()
  body = request(1)
  key = str(body.id)
  (owner.root / f"{key}.json").write_text(json.dumps({
    "id": key, "created_at": _now(), "updated_at": _now(), "phase": "applying",
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": 1, "status": "applying", "version": "1.0", "error": ""}],
  }))
  loop_thread = threading.get_ident()
  seen = []

  def settled_outcomes(reviewed):
    seen.append(("read", threading.get_ident() != loop_thread, key in owner.records))
    return {1: {"status": "failed", "error": batches._NOT_APPLIED}}

  real_apply = batches._apply

  def apply(record, outcomes):
    seen.append(("apply", threading.get_ident() == loop_thread))
    return real_apply(record, outcomes)

  monkeypatch.setattr(batches, "_settled_outcomes", settled_outcomes)
  monkeypatch.setattr(batches, "_apply", apply)
  await owner.retry_start()
  # Read off the loop, before any record is visible; applied on the loop.
  assert seen == [("read", True, False), ("apply", True)]
  assert owner.accepting
  assert owner.snapshot(key)["entries"][0]["status"] == "failed"
  saved = json.loads((owner.root / f"{key}.json").read_text())
  assert (saved["phase"], saved["entries"][0]["status"]) == ("complete", "failed")


async def _expired_failed_batch(owner):
  body = request(1)
  key = str(body.id)
  old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
  owner._save({"id": key, "created_at": old,
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": 1, "status": "failed", "version": "1.0", "error": "x"}]})
  owner.records[key]["updated_at"] = old
  return key


@pytest.mark.asyncio
async def test_a_prune_during_a_refresh_is_never_written_back(tmp_path, monkeypatch):
  from tests.cancel_helpers import BlockedWorker

  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = await _expired_failed_batch(owner)
  worker = BlockedWorker(lambda: {1: {"status": "updated", "version": "2.0", "error": ""}})
  monkeypatch.setattr(batches, "_settled_outcomes", lambda reviewed: worker())
  refresh = asyncio.create_task(owner.refresh_results(key))
  assert await asyncio.to_thread(worker.entered.wait, 5)
  owner._prune_finished()
  assert key not in owner.records
  worker.release.set()
  await refresh
  assert key not in owner.records
  assert key not in owner._save_locks
  assert not (owner.root / f"{key}.json").exists()


@pytest.mark.asyncio
async def test_a_prune_skips_a_batch_whose_save_is_in_flight(tmp_path):
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  key = await _expired_failed_batch(owner)
  lock = owner._save_locks[key] = asyncio.Lock()
  async with lock:
    owner._prune_finished()
    assert key in owner.records and (owner.root / f"{key}.json").exists()
  owner._prune_finished()
  assert key not in owner.records and not (owner.root / f"{key}.json").exists()


def _unknown_history(monkeypatch):
  """Git cannot answer: a timeout, a lock, or damaged metadata."""
  monkeypatch.setattr(batches.app_git, "object_ancestry", lambda *args: None)
  monkeypatch.setattr(batches.app_git, "history_is_complete", lambda *args: False)


def _unreadable_receipt(source_dir):
  """A receipt that exists and cannot be read (here, not a regular file)."""
  pending_update.pending_update_receipt_file(source_dir).mkdir(parents=True)


@pytest.mark.asyncio
async def test_unreadable_settlement_state_is_never_a_failed_update(tmp_path, db, monkeypatch):
  """Restart settles only from reads that worked. An unknown app stays
  unfinished on its own; the owner keeps accepting other apps' batches."""
  app_id = add_app(db, tmp_path, "unknown", version="1.0", upstream_commit=COMMIT,
    source_commit=LANDED)
  healthy = add_app(db, tmp_path, "healthy", version="1.0", source_commit=LANDED)
  body = request(app_id)
  key = str(body.id)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  owner._save({"id": key, "created_at": _now(),
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": app_id, "status": "applying", "version": "1.0", "error": ""}]})

  async def reboot_leaves_it_unfinished():
    reboot = batches.AppUpdateBatches(str(tmp_path), execute=lambda update: asyncio.Event().wait())
    await reboot.start()
    assert reboot.accepting and reboot.failure is None
    saved = json.loads((reboot.root / f"{key}.json").read_text())
    assert (saved["phase"], saved["entries"][0]["status"]) == ("applying", "applying")
    assert saved["entries"][0]["error"] == batches._UNKNOWN_OUTCOME
    return reboot

  with monkeypatch.context() as patch:
    _unknown_history(patch)
    reboot = await reboot_leaves_it_unfinished()
    # An unrelated app's batch is still accepted; this app's stays blocked.
    assert reboot.submit(request(healthy), db)["entries"][0]["status"] == "queued"
    with pytest.raises(HTTPException) as running:
      reboot.submit(request(app_id), db)
    assert running.value.status_code == 409
    await reboot.stop()
  monkeypatch.setattr(batches.app_git, "object_ancestry",
    lambda path, reviewed, accepted: reviewed == COMMIT and accepted == LANDED)
  _unreadable_receipt(tmp_path / "unknown")
  reboot = await reboot_leaves_it_unfinished()

  # Once the reads work again, opening the progress settles what happened.
  pending_update.pending_update_receipt_file(tmp_path / "unknown").rmdir()
  await reboot.refresh_results(key, db)
  entry = reboot.snapshot(key)["entries"][0]
  assert (entry["status"], entry["version"], entry["error"]) == ("updated", "1.0", "")
  assert reboot.snapshot(key)["phase"] == "complete"


def test_a_reviewed_commit_never_fetched_proves_not_applied(tmp_path, db):
  """Git cannot compare a commit it lacks; a full served history without it
  still proves the update did not land."""
  repo = tmp_path / "unfetched"
  subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
  subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
    "commit", "-q", "--allow-empty", "-m", "served"], check=True)
  served = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True).stdout.strip()
  app = models.App(name="Unfetched", slug="unfetched", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, version="1.0")
  db.add(app)
  db.commit()
  assert batches.app_git.object_ancestry(str(repo), COMMIT, served) is None
  outcome = batches._settled_outcomes({app.id: COMMIT})[app.id]
  assert (outcome["status"], outcome["error_code"]) == ("failed", "update_not_applied")


@pytest.mark.asyncio
async def test_unreadable_state_keeps_saved_results_and_unfinished_entries(
  tmp_path, db, git_state, monkeypatch,
):
  """Refresh and shutdown keep what they knew when a read fails."""
  conflicted = add_app(db, tmp_path, "conflicted", version="1.0", source_commit="1" * 40)
  running = add_app(db, tmp_path, "running", version="1.0", source_commit="1" * 40)
  started = asyncio.Event()

  async def execute(update):
    if update["app_id"] == conflicted:
      git_state.add(tmp_path / "conflicted", conflicted)
      return {"status": "conflict"}
    started.set()
    await asyncio.Event().wait()

  owner = batches.AppUpdateBatches(str(tmp_path), execute=execute)
  await owner.start()
  conflict_key = owner.submit(request(conflicted), db)["id"]
  running_key = owner.submit(request(running), db)["id"]
  await started.wait()
  while owner.snapshot(conflict_key)["phase"] != "complete":
    await asyncio.sleep(0)
  git_state.clear()
  _unreadable_receipt(tmp_path / "conflicted")
  await owner.refresh_results(conflict_key, db)
  assert owner.snapshot(conflict_key)["entries"][0]["status"] == "conflict"

  _unknown_history(monkeypatch)
  await owner.stop()
  saved = json.loads((owner.root / f"{running_key}.json").read_text())
  assert saved["entries"][0]["status"] == "applying"  # boot settles it


def _git(repo, *args):
  return subprocess.run(
    ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
    check=True, capture_output=True, text=True,
  ).stdout.strip()


def test_a_missing_history_object_is_unknown_not_not_applied(tmp_path, db):
  """A served tip that resolves is not complete history: a missing reviewed
  ancestor must not become a terminal "not applied"."""
  repo = tmp_path / "damaged"
  repo.mkdir()
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "reviewed")
  reviewed = _git(repo, "rev-parse", "HEAD")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "served")
  served = _git(repo, "rev-parse", "HEAD")
  app = models.App(name="Damaged", slug="damaged", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, version="1")
  db.add(app)
  db.commit()
  assert batches._settled_outcomes({app.id: reviewed})[app.id]["status"] == "updated"

  (repo / ".git" / "objects" / reviewed[:2] / reviewed[2:]).unlink()
  assert batches.app_git.object_ancestry(str(repo), reviewed, served) is None
  assert batches._settled_outcomes({app.id: reviewed})[app.id] == {"error": batches._UNKNOWN_OUTCOME}


def test_a_shallow_served_history_is_unknown(tmp_path, db):
  origin = tmp_path / "origin"
  origin.mkdir()
  _git(origin, "init", "-q", "-b", "main")
  for message in ("one", "two"):
    _git(origin, "commit", "-q", "--allow-empty", "-m", message)
  shallow = tmp_path / "shallow"
  subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
    check=True)
  served = _git(shallow, "rev-parse", "HEAD")
  assert batches.app_git.history_is_complete(str(shallow), served) is False
  with pytest.raises(batches.SettlementUnreadable):
    batches._contains(str(shallow), COMMIT, served)


@pytest.mark.parametrize("content", [
  b'{"schema":4,',  # truncated write
  b"\xff\xfe",  # not UTF-8
  b"[]",  # not an object
  json.dumps({"schema": 4, "app_id": 1, "upstream_commit": COMMIT}).encode(),
])
def test_an_invalid_existing_receipt_is_unknown_not_absent(tmp_path, content):
  path = pending_update.pending_update_receipt_file(tmp_path)
  path.parent.mkdir(parents=True)
  path.write_bytes(content)
  with pytest.raises(batches.SettlementUnreadable):
    batches._pending_receipt(str(tmp_path), 1, COMMIT)
  # Existing readers keep their contract: anything unusable reads as none.
  assert pending_update.read_pending_conflict_update_receipt(
    tmp_path, app_id=1, upstream_commit=COMMIT,
  ) is None


def test_a_receipt_for_another_update_or_none_is_proven_absent(tmp_path):
  assert batches._pending_receipt(str(tmp_path), 1, COMMIT) is None
  path = pending_update.pending_update_receipt_file(tmp_path)
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps({"schema": 4, "app_id": 1, "upstream_commit": "b" * 40}))
  assert batches._pending_receipt(str(tmp_path), 1, COMMIT) is None


def test_a_shallow_negative_answer_is_unknown_even_with_both_commits(tmp_path, db):
  """Across a shallow boundary Git answers "not an ancestor" for a commit it
  cannot reach; that is not proof the update did not land."""
  origin = tmp_path / "origin"
  origin.mkdir()
  _git(origin, "init", "-q", "-b", "main")
  _git(origin, "commit", "-q", "--allow-empty", "-m", "reviewed")
  reviewed = _git(origin, "rev-parse", "HEAD")
  _git(origin, "commit", "-q", "--allow-empty", "-m", "served")
  served = _git(origin, "rev-parse", "HEAD")
  repo = tmp_path / "shallow"
  subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(repo)],
    check=True)
  # An earlier preview can supply the object without deepening the tip.
  _git(repo, "fetch", "-q", "--depth", "1", "origin", reviewed)
  assert batches.app_git.object_ancestry(str(repo), reviewed, served) is False
  app = models.App(name="Shallow", slug="shallow", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, version="1")
  db.add(app)
  db.commit()
  assert batches._settled_outcomes({app.id: reviewed})[app.id] == {"error": batches._UNKNOWN_OUTCOME}


def test_an_unrelated_commit_on_complete_history_is_not_applied(tmp_path, db):
  """The negative answer stays usable when the history is complete."""
  repo = tmp_path / "complete"
  repo.mkdir()
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "served")
  served = _git(repo, "rev-parse", "HEAD")
  _git(repo, "checkout", "-q", "--orphan", "other")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "reviewed elsewhere")
  reviewed = _git(repo, "rev-parse", "HEAD")
  assert batches.app_git.object_ancestry(str(repo), reviewed, served) is False
  app = models.App(name="Complete", slug="complete", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, version="1.0")
  db.add(app)
  db.commit()
  outcome = batches._settled_outcomes({app.id: reviewed})[app.id]
  assert (outcome["status"], outcome["error_code"]) == ("failed", "update_not_applied")


def test_a_file_where_the_receipt_directory_belongs_is_unknown(tmp_path, db, monkeypatch):
  path = pending_update.pending_update_receipt_file(tmp_path)
  path.parent.parent.mkdir(parents=True)
  path.parent.write_bytes(b"corrupted receipt directory")
  assert pending_update.pending_conflict_update_receipt_present(tmp_path)
  monkeypatch.setattr(batches.app_git, "object_ancestry", lambda *_: False)
  monkeypatch.setattr(batches.app_git, "history_is_complete", lambda *_: True)
  app = models.App(name="Receipt directory", slug="receipt-dir", jsx_source="export default 1",
    source_dir=str(tmp_path), source_commit="b" * 40, version="1")
  db.add(app)
  db.commit()
  assert batches._settled_outcomes({app.id: COMMIT})[app.id] == {"error": batches._UNKNOWN_OUTCOME}


def test_a_commit_graph_cannot_hide_a_missing_history_object(tmp_path, db):
  """The completeness walk reads real objects, not the commit-graph cache."""
  repo = tmp_path / "cached"
  repo.mkdir()
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "reviewed")
  reviewed = _git(repo, "rev-parse", "HEAD")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "served")
  served = _git(repo, "rev-parse", "HEAD")
  _git(repo, "commit-graph", "write", "--reachable")
  (repo / ".git" / "objects" / reviewed[:2] / reviewed[2:]).unlink()
  app = models.App(name="Cached", slug="cached", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, version="1")
  db.add(app)
  db.commit()
  assert batches.app_git.object_ancestry(str(repo), reviewed, served) is None
  assert batches.app_git.commit_object_absent(str(repo), reviewed) is True
  assert batches.app_git.history_is_complete(str(repo), served) is False
  assert batches._settled_outcomes({app.id: reviewed})[app.id] == {"error": batches._UNKNOWN_OUTCOME}


def test_failed_git_reads_never_certify_absence(tmp_path, monkeypatch):
  _git(tmp_path, "init", "-q")
  original = batches.app_git._run

  def timeout(path, *args, **kwargs):
    if "rev-list" in args:
      raise subprocess.TimeoutExpired(["git", "rev-list"], 30)
    return original(path, *args, **kwargs)

  monkeypatch.setattr(batches.app_git, "_run", timeout)
  assert batches.app_git.history_is_complete(str(tmp_path), "a" * 40) is False

  def broken(*_args, **_kwargs):
    raise OSError("cannot execute git")

  monkeypatch.setattr(batches.app_git, "_run", broken)
  assert batches.app_git.commit_object_absent(str(tmp_path), "a" * 40) is False


def _corrupt_cached_first_parent(repo, commit):
  """Damage one commit's first-parent entry in the commit-graph, leaving every
  Git object intact (checksum deliberately stale: real corruption)."""
  path = repo / ".git" / "objects" / "info" / "commit-graph"
  data = bytearray(path.read_bytes())
  assert data[:6] == b"CGPH\x01\x01"
  chunks = {
    bytes(data[8 + i * 12:12 + i * 12]): struct.unpack_from(">Q", data, 12 + i * 12)[0]
    for i in range(data[6])
  }
  pos = data.find(bytes.fromhex(commit), chunks[b"OIDL"], chunks[b"CDAT"])
  assert pos >= 0 and (pos - chunks[b"OIDL"]) % 20 == 0
  index = (pos - chunks[b"OIDL"]) // 20
  struct.pack_into(">I", data, chunks[b"CDAT"] + 36 * index + 20, 0x70000000)
  path.chmod(0o600)
  path.write_bytes(data)


def test_a_corrupt_cached_parent_cannot_make_a_landed_update_not_applied(tmp_path, db):
  """The cached graph says "not an ancestor"; the real objects say it landed."""
  repo = tmp_path / "badgraph"
  repo.mkdir()
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "reviewed")
  reviewed = _git(repo, "rev-parse", "HEAD")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "middle")
  middle = _git(repo, "rev-parse", "HEAD")
  _git(repo, "commit", "-q", "--allow-empty", "-m", "served")
  served = _git(repo, "rev-parse", "HEAD")
  _git(repo, "commit-graph", "write", "--reachable")
  _corrupt_cached_first_parent(repo, middle)
  assert batches.app_git.ref_is_ancestor(str(repo), reviewed, served) is False
  assert batches.app_git.object_ancestry(str(repo), reviewed, served) is True
  assert batches.app_git.history_is_complete(str(repo), served) is True
  app = models.App(name="Bad graph", slug="bad-graph", jsx_source="export default 1",
    source_dir=str(repo), source_commit=served, version="1")
  db.add(app)
  db.commit()
  assert batches._settled_outcomes({app.id: reviewed})[app.id]["status"] == "updated"


@pytest.mark.asyncio
async def test_a_shallow_legacy_app_repo_blocks_only_its_own_entry(tmp_path, db):
  """An app left shallow by an interrupted update stays unknown on its own:
  boot keeps accepting, and another app's batch is admitted."""
  origin = tmp_path / "origin"
  origin.mkdir()
  _git(origin, "init", "-q", "-b", "main")
  for message in ("one", "two"):
    _git(origin, "commit", "-q", "--allow-empty", "-m", message)
  shallow = tmp_path / "shallow"
  subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{origin}", str(shallow)],
    check=True)
  served = _git(shallow, "rev-parse", "HEAD")
  legacy = models.App(name="Legacy", slug="legacy", jsx_source="export default 1",
    source_dir=str(shallow), source_commit=served, version="1.0")
  db.add(legacy)
  db.commit()
  healthy = add_app(db, tmp_path, "healthy", version="1.0", source_commit=served)
  body = request(legacy.id)  # reviewed COMMIT was never fetched
  key = str(body.id)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  owner._save({"id": key, "created_at": _now(),
    "updates": [u.model_dump() for u in body.updates],
    "entries": [{"app_id": legacy.id, "status": "applying", "version": "1.0", "error": ""}]})

  reboot = batches.AppUpdateBatches(
    str(tmp_path), execute=lambda update: asyncio.Event().wait(),
  )
  await reboot.start()
  await reboot.retry_start()
  assert reboot.accepting and reboot.failure is None
  entry = reboot.snapshot(key)["entries"][0]
  assert (entry["status"], entry["error"]) == ("applying", batches._UNKNOWN_OUTCOME)
  other = reboot.submit(request(healthy), db)
  assert other["entries"][0]["app_id"] == healthy
  await reboot.stop()


def test_a_receipt_naming_the_commit_is_a_conflict_whatever_the_history(
  tmp_path, db, monkeypatch,
):
  app_id = add_app(db, tmp_path, "pending", version="1.0", source_commit=LANDED)
  _Receipts().add(tmp_path / "pending", app_id)
  _unknown_history(monkeypatch)
  assert batches._settled_outcomes({app_id: COMMIT})[app_id]["status"] == "conflict"


@pytest.mark.asyncio
async def test_refresh_settles_every_entry_it_can_beside_an_unknown_one(
  tmp_path, db, git_state, monkeypatch,
):
  """One app's unreadable receipt no longer stops its siblings re-settling."""
  unknown = add_app(db, tmp_path, "unknown", version="1.0", source_commit="1" * 40)
  landed = add_app(db, tmp_path, "landed", version="2.0", source_commit=LANDED)
  body = request(unknown, landed)
  key = str(body.id)
  owner = batches.AppUpdateBatches(str(tmp_path))
  await owner.start()
  owner._save({"id": key, "created_at": _now(),
    "updates": [u.model_dump() for u in body.updates],
    "entries": [
      {"app_id": app_id, "status": "conflict", "version": "1.0", "error": ""}
      for app_id in (unknown, landed)
    ]})
  _unreadable_receipt(tmp_path / "unknown")

  await owner.refresh_results(key, db)

  entries = {e["app_id"]: e for e in owner.snapshot(key)["entries"]}
  assert (entries[unknown]["status"], entries[unknown]["error"]) == ("conflict", "")
  assert (entries[landed]["status"], entries[landed]["version"]) == ("updated", "2.0")


def test_an_unknown_app_logs_one_line_naming_the_app_and_cause(
  tmp_path, db, caplog,
):
  app_id = add_app(db, tmp_path, "unread", version="1.0", source_commit=LANDED)
  _unreadable_receipt(tmp_path / "unread")
  with caplog.at_level(logging.WARNING, logger=batches.log.name):
    outcome = batches._settled_outcomes({app_id: COMMIT})[app_id]
  assert outcome == {"error": batches._UNKNOWN_OUTCOME}
  [entry] = [r for r in caplog.records if "is unknown" in r.getMessage()]
  assert f"app {app_id} update to {COMMIT}" in entry.getMessage()
  assert "pending update receipt" in entry.getMessage()
  assert "Is a directory" in entry.getMessage()
  assert entry.exc_info is None
