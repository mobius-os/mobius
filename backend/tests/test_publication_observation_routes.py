"""Signed publication settlement uses no source or public mutation path."""
import asyncio
import copy
import json
import subprocess
import threading
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from app import contribution_runtime, fs_locks, github_contribution_git, models, settled
from app.config import get_settings
from app.database import SessionLocal, checked_out_connections
from app.main import app
from app.routes import github as routes
from tests.test_github_routes import _app_token, _write_contribution


@pytest.fixture(autouse=True)
def isolated_private_receipts(monkeypatch, tmp_path):
  monkeypatch.setattr(contribution_runtime, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path)))


@pytest.fixture
def observation(client, owner_token, monkeypatch):
  app_id, token = _app_token(client, owner_token, github_access=True)
  repo = Path(get_settings().data_dir) / "contrib" / "observer"
  repo.mkdir(parents=True, exist_ok=True)
  record = {
    "id": "observe-me", "type": "pr", "status": "submitting", "repo": "owner/demo",
    "submitter": "contribute-button", "source_chat_id": "preserved-chat",
    "plan": {"action": "pr", "repo": "owner/demo", "repo_path": str(repo),
             "branch": "topic", "base_branch": "main", "head_sha": "a" * 40,
             "title": "Reviewed", "body_draft": "Exact body", "labels": []},
    "quality_review": {"state": "all_clear", "reviewed_head_sha": "a" * 40},
    "autopilot": {"enabled": False, "state": "stopped", "pending_question": "Do not resume"},
  }
  _write_contribution(app_id, record["id"], record)
  path, _ = routes._record_paths(app_id, record["id"])
  owner = routes._PersonalAttemptOwner(app_id=app_id, record_id=record["id"], record_path=path,
                                      claimed=record, action_input={"action": "submit", "publication_stage": "draft"})
  effect = {"action": "create_pr", "repo": "owner/demo", "head_repository": "fork/demo",
            "branch": "topic", "head_sha": "a" * 40, "base_branch": "main",
            "title": "Reviewed", "body": "Exact body"}
  patch = {"last_submit_push_sha": "a" * 40, "last_submit_stage": "pushed", "head_repository": "fork/demo"}
  owner.event("pr_ambiguous", effect, patch)
  failed = {**record, **patch, "status": "prepared", "last_submit_error": "Unknown create",
            "last_submit_error_code": "create_unconfirmed", "last_submit_error_detail": "lost reply"}
  routes._write_record(path, failed)
  live = {"number": 7, "html_url": "https://github.com/owner/demo/pull/7", "title": "Reviewed",
          "body": "Exact body", "state": "open", "draft": True, "merged_at": None,
          "head": {"ref": "topic", "sha": "a" * 40, "repo": {"full_name": "fork/demo"}},
          "base": {"ref": "main", "repo": {"full_name": "owner/demo"}}}
  calls = []
  def remote(repo, *args, **kwargs):
    assert args[0] == "api" and len(args) == 2, args
    calls.append(args[1])
    if "/git/ref/heads/" in args[1]: value = {"object": {"sha": "a" * 40}}
    elif "/pulls?" in args[1]: value = [live]
    elif args[1].endswith("/pulls/7"): value = live
    elif args[1].endswith("/labels"): value = []
    else: raise AssertionError(args)
    return subprocess.CompletedProcess(args, 0, json.dumps(value), "")
  monkeypatch.setattr(github_contribution_git, "_gh", remote)
  monkeypatch.setattr(routes.github_auth, "read_state", lambda: {"login": "fork", "user_id": 42})
  def forbidden(*args, **kwargs): raise AssertionError("Observation entered a mutation/source path")
  for name in ("_submit_prepared_pr", "_advance_merged_parent_successor", "_land_reviewed_stack",
               "_apply_reviewed_pr_labels", "_personal_resume_allowed", "_claim_record",
               "_record_pending_equivalence_locked"):
    monkeypatch.setattr(routes, name, forbidden)
  monkeypatch.setattr(github_contribution_git, "_git", forbidden)
  return {"app_id": app_id, "headers": {"Authorization": f"Bearer {token}"},
          "record": failed, "path": path, "owner": owner, "effect": effect,
          "patch": patch, "live": live, "calls": calls,
          "url": f"/api/github/contributions/{app_id}/observe-me/observe"}


def receipt_path(f):
  return contribution_runtime.personal_attempt_path(f["app_id"], "observe-me")


def test_exact_observation_persists_record_retires_receipt_and_survives_reload(client, observation):
  f = observation
  response = client.post(f["url"], headers=f["headers"])
  assert response.status_code == 200, response.text
  body = response.json()
  assert set(body) == {"outcome", "records", "message"}
  assert body["outcome"] == "settled", body
  saved = json.loads(f["path"].read_text())
  assert saved == body["records"][0]
  assert saved["status"] == "draft" and saved["number"] == 7
  assert not any(key.startswith("last_submit_error") for key in saved)
  for key in ("plan", "quality_review", "autopilot", "source_chat_id"):
    assert saved[key] == f["record"][key]
  assert not receipt_path(f).exists()
  before = f["path"].read_bytes()
  again = client.post(f["url"], headers=f["headers"]).json()
  assert again["outcome"] != "settled"  # no invented second proof
  assert f["path"].read_bytes() == before


@pytest.mark.parametrize("damage", ["missing", "signature", "unicode-signature", "wrong-app", "wrong-record", "wrong-action", "wrong-claim", "review-changed"])
def test_invalid_or_replaced_claim_never_settles(client, observation, damage):
  f = observation
  receipt = json.loads(receipt_path(f).read_text())
  if damage == "missing": receipt_path(f).unlink()
  elif damage == "review-changed":
    record = json.loads(f["path"].read_text()); record["quality_review"]["state"] = "changes_needed"
    routes._write_record(f["path"], record)
  else:
    if damage == "signature": receipt["hmac_sha256"] = "invalid"
    elif damage == "unicode-signature": receipt["hmac_sha256"] = "Ω" * 64
    elif damage == "wrong-app": receipt["app_id"] += 1
    elif damage == "wrong-record": receipt["record_id"] = "another"
    elif damage == "wrong-action": receipt["action_input"]["action"] = "update_existing"
    elif damage == "wrong-claim": receipt["claim_sha256"] = "f" * 64
    if damage not in {"signature", "unicode-signature"}: receipt = routes._signed_personal_attempt(receipt)
    receipt_path(f).write_text(json.dumps(receipt))
  before = f["path"].read_bytes()
  result = client.post(f["url"], headers=f["headers"]).json()
  assert result["outcome"] in {"changed", "unavailable"}
  assert f["path"].read_bytes() == before and not f["calls"]


@pytest.mark.parametrize("field", ["head", "base", "text", "labels"])
def test_partial_and_conflicting_remote_facts_keep_lock_and_receipt(client, observation, field):
  f = observation
  if field == "head": f["live"]["head"]["sha"] = "b" * 40
  elif field == "base": f["live"]["base"]["ref"] = "old-base"
  elif field == "text": f["live"]["body"] = "not reviewed"
  else:
    receipt = json.loads(receipt_path(f).read_text())
    receipt["claim_input"]["plan"]["labels"] = ["reviewed"]
    receipt["claim_sha256"] = routes._personal_publication_input_sha256(receipt["claim_input"])
    receipt_path(f).write_text(json.dumps(routes._signed_personal_attempt(receipt)))
    record = json.loads(f["path"].read_text()); record["plan"]["labels"] = ["reviewed"]
    routes._write_record(f["path"], record)
  before = f["path"].read_bytes()
  response = client.post(f["url"], headers=f["headers"])
  assert response.json()["outcome"] in {"pending", "changed"}, response.text
  assert f["path"].read_bytes() == before and receipt_path(f).exists()


@pytest.mark.parametrize("change", ["record", "receipt", "nonce", "actor"])
def test_remote_read_rechecks_full_record_receipt_nonce_and_actor(client, observation, monkeypatch, change):
  f = observation
  original = routes.observe_publication_effect
  def proof(record, attempt):
    result = original(record, attempt)
    assert result["outcome"] == "settled"
    if change == "record":
      row = json.loads(f["path"].read_text()); row["last_submit_error_detail"] = "newer diagnostic"
      routes._write_record(f["path"], row)
    elif change == "receipt":
      attempt = {**attempt, "updated_at": "new-generation"}
      receipt_path(f).write_text(json.dumps(routes._signed_personal_attempt(attempt)))
    elif change == "actor":
      monkeypatch.setattr(routes.github_auth, "read_state", lambda: {"login": "different"})
    else:
      with SessionLocal() as db:
        row = db.get(models.App, f["app_id"]); row.token_nonce = "revoked"; db.commit()
    return result
  monkeypatch.setattr(routes, "observe_publication_effect", proof)
  response = client.post(f["url"], headers=f["headers"])
  if change == "nonce": assert response.status_code == 404
  else: assert response.json()["outcome"] == "changed"
  assert receipt_path(f).exists()
  assert json.loads(f["path"].read_text())["status"] == "prepared"


@pytest.mark.parametrize("crash", ["before-ledger", "after-ledger"])
def test_signed_local_settlement_resumes_after_crash_without_remote_replay(client, observation, monkeypatch, crash):
  f = observation
  original = routes._write_record
  count = 0
  def write(path, record):
    nonlocal count
    count += 1
    if crash == "after-ledger": original(path, record)
    raise OSError("simulated process exit")
  monkeypatch.setattr(routes, "_write_record", write)
  with pytest.raises(OSError, match="simulated"):
    client.post(f["url"], headers=f["headers"])
  assert count == 1
  attempt = json.loads(receipt_path(f).read_text())
  assert attempt["observation_settlement"]
  monkeypatch.setattr(routes, "_write_record", original)
  monkeypatch.setattr(routes, "observe_publication_effect", lambda *a: pytest.fail("Crash replay reread GitHub"))
  response = client.post(f["url"], headers=f["headers"])
  assert response.json()["outcome"] == "settled", response.text
  assert not receipt_path(f).exists()


def test_observation_body_cannot_nominate_an_action_or_target(client, observation):
  f = observation
  response = client.post(f["url"], headers=f["headers"], json={"action": "submit", "repo": "foreign/repo"})
  assert response.status_code == 400 and not f["calls"]


def test_remote_observation_holds_only_action_mutex(client, observation, monkeypatch):
  f = observation
  original = routes.observe_publication_effect
  def proof(*args):
    assert checked_out_connections() == 0
    # Locks are represented by the owning pool, tested through an independent
    # event-loop acquisition below instead of inspecting private lock fields.
    return original(*args)
  monkeypatch.setattr(routes, "observe_publication_effect", proof)
  assert client.post(f["url"], headers=f["headers"]).json()["outcome"] == "settled"


@pytest.mark.asyncio
async def test_duplicate_observers_serialize_and_release_storage_during_remote_read(observation, monkeypatch):
  f = observation
  started, release = threading.Event(), threading.Event()
  original = routes.observe_publication_effect
  count = 0
  def proof(*args):
    nonlocal count
    count += 1; started.set()
    assert release.wait(5)
    return original(*args)
  monkeypatch.setattr(routes, "observe_publication_effect", proof)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
    first = asyncio.create_task(client.post(f["url"], headers=f["headers"]))
    assert await asyncio.to_thread(started.wait, 5)
    second = asyncio.create_task(client.post(f["url"], headers=f["headers"]))
    async with asyncio.timeout(2):
      async with fs_locks.app_storage_lock(f["app_id"]): pass
      async with fs_locks.source_dir_lock(f["record"]["plan"]["repo_path"]): pass
    assert not second.done()
    release.set()
    replies = await asyncio.gather(first, second)
  assert [r.json()["outcome"] for r in replies] == ["settled", "unavailable"]
  assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["push", "create", "labels", "retarget"])
async def test_unchanged_inflight_public_owner_blocks_observation(observation, monkeypatch, phase):
  f = observation
  # This is the same admission mutex attached to every owner endpoint below;
  # the journal is deliberately unchanged throughout the live effect window.
  sender = routes._serialize_ready_action(f["app_id"], "observe-me")
  await anext(sender)
  called = threading.Event()
  original = routes.observe_publication_effect
  def proof(*args): called.set(); return original(*args)
  monkeypatch.setattr(routes, "observe_publication_effect", proof)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
    observer = asyncio.create_task(client.post(f["url"], headers=f["headers"]))
    await asyncio.sleep(.02)
    assert not observer.done() and not called.is_set(), phase
    await sender.aclose()
    assert (await observer).json()["outcome"] == "settled"


@pytest.mark.parametrize("suffix", ["/{record_id}/submit", "/{record_id}/update-existing",
  "/{record_id}/update", "/{record_id}/ready", "/submit-stack", "/update-stack", "/land-stack"])
def test_every_public_owner_shares_observer_admission_mutex(suffix):
  route = next(r for r in routes.router.routes if r.path == "/api/github/contributions/{app_id}" + suffix)
  expected = routes._serialize_stack_action if suffix.endswith("-stack") else routes._serialize_ready_action
  assert any(dependency.call is expected for dependency in route.dependant.dependencies)


@pytest.mark.asyncio
async def test_overlapping_stack_locks_have_one_stable_order():
  async with routes._publication_action_locks(1, ["z", "a"]):
    waiting = asyncio.create_task(_take_stack(["a", "z"]))
    await asyncio.sleep(0)
    assert not waiting.done()
  assert await asyncio.wait_for(waiting, 1) == "done"


async def _take_stack(ids):
  async with routes._publication_action_locks(1, ids): return "done"


@pytest.mark.asyncio
async def test_cancelled_sender_retains_action_mutex_until_effect_worker_finishes():
  entered, finish = threading.Event(), threading.Event()
  def public_effect():
    entered.set()
    assert finish.wait(5)
  async def sender():
    async with routes._publication_action_locks(1, ["cancelled"]):
      await settled.run_settled(public_effect)
  task = asyncio.create_task(sender())
  assert await asyncio.to_thread(entered.wait, 5)
  task.cancel()
  await asyncio.sleep(0)
  task.cancel()  # even a second cancellation cannot release the live worker
  async def observer():
    async with routes._publication_action_locks(1, ["cancelled"]): return "observed"
  waiting = asyncio.create_task(observer())
  await asyncio.sleep(.02)
  assert not task.done() and not waiting.done()
  finish.set()
  with pytest.raises(asyncio.CancelledError): await task
  assert await asyncio.wait_for(waiting, 1) == "observed"


@pytest.mark.parametrize("endpoint,body", [
  ("submit", {}), ("update-existing", {}),
  ("ready", {"expected_head_sha": "a" * 40}),
])
def test_public_owner_cannot_consume_a_crashed_local_settlement(client, observation, monkeypatch, endpoint, body):
  f = observation
  original = routes._write_record
  def crash(*args): raise OSError("exit before ledger")
  monkeypatch.setattr(routes, "_write_record", crash)
  with pytest.raises(OSError): client.post(f["url"], headers=f["headers"])
  monkeypatch.setattr(routes, "_write_record", original)
  response = client.post(f["url"].removesuffix("observe") + endpoint, headers=f["headers"], json=body)
  assert response.status_code in {400, 409}, response.text
  assert json.loads(receipt_path(f).read_text())["observation_settlement"]
  assert client.post(f["url"], headers=f["headers"]).json()["outcome"] == "settled"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["push", "create", "labels", "retarget"])
async def test_actual_submit_route_owns_unchanged_effect_window(observation, monkeypatch, phase):
  f = observation
  entered, finish = threading.Event(), threading.Event()
  # Exercise the real HTTP owner and its settlement, replacing only its
  # preflight/public effect with a deterministic synthetic GitHub operation.
  monkeypatch.setattr(routes, "confirmed_publication_records", lambda *a: {})
  monkeypatch.setattr(routes, "_assert_confirmed_publication", lambda *a, **k: None)
  monkeypatch.setattr(routes, "_personal_resume_allowed", lambda **k: False)
  def claim(**kwargs):
    current = routes._read_record(f["path"])
    claimed = {**current, "status": "submitting"}
    kwargs["before_claim_write"](claimed, f["path"], False, current)
    routes._write_record(f["path"], claimed)
    return claimed, f["path"], f["path"].with_suffix(".diff")
  monkeypatch.setattr(routes, "_claim_record", claim)
  monkeypatch.setattr(routes, "_assert_personal_publication_source", lambda *a: None)
  async def no_equivalence(*a, **k): return None
  monkeypatch.setattr(routes, "_record_pending_equivalence_locked", no_equivalence)
  receipt_path(f).unlink()
  def effect(record, diff, **kwargs):
    kwargs["attempt_event"]("pr_ambiguous", f["effect"], f["patch"])
    entered.set()
    assert finish.wait(5), phase
    kwargs["attempt_event"]("complete", {**f["effect"], "number": 7}, f["patch"])
    return f["live"]["html_url"], 7, f["patch"]
  monkeypatch.setattr(routes, "_submit_prepared_pr", effect)
  observed = threading.Event()
  def proof(*args): observed.set(); pytest.fail("Active/settled sender must own this outcome")
  monkeypatch.setattr(routes, "observe_publication_effect", proof)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
    sender = asyncio.create_task(client.post(f["url"].removesuffix("observe") + "submit", headers=f["headers"]))
    assert await asyncio.to_thread(entered.wait, 5)
    observer = asyncio.create_task(client.post(f["url"], headers=f["headers"]))
    await asyncio.sleep(.02)
    assert not observer.done() and not observed.is_set()
    finish.set()
    sent, seen = await asyncio.gather(sender, observer)
  assert sent.status_code == 200, sent.text
  assert sent.json()["record"]["status"] == "draft"
  assert seen.json()["outcome"] == "unavailable"
  assert not receipt_path(f).exists()


@pytest.mark.asyncio
async def test_public_request_waiting_on_observer_does_not_hold_auth_database_lease(observation, monkeypatch):
  f = observation
  from app.github_contributions import _claim_record
  monkeypatch.setattr(routes, "_claim_record", _claim_record)
  monkeypatch.setattr(routes, "_personal_resume_allowed", lambda **kwargs: False)
  entered, finish = threading.Event(), threading.Event()
  original = routes.observe_publication_effect
  def proof(*args):
    entered.set(); assert finish.wait(5)
    return original(*args)
  monkeypatch.setattr(routes, "observe_publication_effect", proof)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
    observing = asyncio.create_task(client.post(f["url"], headers=f["headers"]))
    assert await asyncio.to_thread(entered.wait, 5)
    sending = asyncio.create_task(client.post(f["url"].removesuffix("observe") + "submit", headers=f["headers"]))
    await asyncio.sleep(.05)
    assert not sending.done() and checked_out_connections() == 0
    finish.set()
    seen, sent = await asyncio.gather(observing, sending)
  assert seen.json()["outcome"] == "settled"
  assert sent.status_code == 409  # old confirmation cannot publish settled work


def test_owner_saved_complete_create_before_receipt_retirement_is_observable(client, observation):
  f = observation
  # Reconstruct the owning sender's exact complete event then successful
  # ledger write; the crash leaves only its original receipt, no observer WAL.
  claim = copy.deepcopy(f["record"])
  claim["status"] = "submitting"
  routes._write_record(f["path"], claim)
  owner = routes._PersonalAttemptOwner(app_id=f["app_id"], record_id=claim["id"], record_path=f["path"],
    claimed=claim, action_input={"action": "submit", "publication_stage": "draft"})
  patch = {**f["patch"], "publication_stage": "draft"}
  owner.event("complete", {**f["effect"], "url": f["live"]["html_url"], "number": 7}, patch)
  saved = routes._mark_submit_success(record_path=f["path"], record=claim,
                                      pr_url=f["live"]["html_url"], number=7, record_patch=patch)
  response = client.post(f["url"], headers=f["headers"])
  assert response.json()["outcome"] == "settled", response.text
  assert response.json()["records"][0]["submitted_at"] == saved["submitted_at"]
  assert not receipt_path(f).exists()


def test_observed_labels_use_canonical_name_and_clear_only_confirmed_label_diagnostic(client, observation, monkeypatch):
  f = observation
  receipt = json.loads(receipt_path(f).read_text())
  receipt["claim_input"]["plan"]["labels"] = ["bug"]
  receipt["claim_sha256"] = routes._personal_publication_input_sha256(receipt["claim_input"])
  receipt_path(f).write_text(json.dumps(routes._signed_personal_attempt(receipt)))
  row = routes._read_record(f["path"])
  row["plan"]["labels"] = ["bug"]
  row.update(last_submit_labels_note="Labels unconfirmed", last_submit_labels_applied=[])
  routes._write_record(f["path"], row)
  remote = github_contribution_git._gh
  def labels(repo, *args, **kwargs):
    if args[1].endswith("/labels"):
      return subprocess.CompletedProcess(args, 0, json.dumps([{"name": "Bug"}]), "")
    return remote(repo, *args, **kwargs)
  monkeypatch.setattr(github_contribution_git, "_gh", labels)
  response = client.post(f["url"], headers=f["headers"])
  assert response.json()["outcome"] == "settled", response.text
  saved = response.json()["records"][0]
  assert saved["last_submit_labels_requested"] == ["bug"]
  assert saved["last_submit_labels_applied"] == ["Bug"]
  assert "last_submit_labels_note" not in saved


def _crash_after_journal(client, f, monkeypatch, url=None):
  original = routes._write_record
  def crash(*a): raise OSError("process exit after WAL")
  monkeypatch.setattr(routes, "_write_record", crash)
  with pytest.raises(OSError):
    client.post(url or f["url"], headers=f["headers"])
  monkeypatch.setattr(routes, "_write_record", original)


@pytest.fixture
def stack(observation, monkeypatch):
  """Two linked stack layers, each with its own ambiguous signed create."""
  f = observation
  records = []
  for i in range(2):
    row = copy.deepcopy(f["record"])
    row.update({"id": f"layer-{i + 1}", "status": "submitting", "head_repository": "owner/demo"})
    row["plan"].update({"head_sha": ("c" if i == 0 else "d") * 40,
                        "base_sha": ("b" if i == 0 else "c") * 40,
                        "branch": f"stack/group/{i + 1}",
                        "stack": {"id": "group", "position": i + 1, "total": 2,
                                  "base_branch": "main" if i == 0 else "stack/group/1",
                                  "parent_record_id": "" if i == 0 else "layer-1"}})
    records.append(row)
  ids = [row["id"] for row in records]
  for row in records:
    _write_contribution(f["app_id"], row["id"], row)
    path = routes._record_paths(f["app_id"], row["id"])[0]
    # Senders admit a complete stack in any request order.
    owner = routes._PersonalAttemptOwner(app_id=f["app_id"], record_id=row["id"], record_path=path,
      claimed=row, action_input={"action": "submit_stack", "record_ids": ids[::-1], "publication_stage": "draft"})
    owner.event("pr_ambiguous", {
      "action": "create_pr", "repo": row["repo"], "head_repository": row["head_repository"],
      "branch": row["plan"]["branch"], "head_sha": row["plan"]["head_sha"],
      "base_branch": row["plan"]["stack"]["base_branch"], "title": row["plan"]["title"],
      "body": row["plan"]["body_draft"]}, {})
    row["status"] = "prepared"
    routes._write_record(path, row)
  lives = {row["plan"]["branch"]: {
    "number": 20 + i, "html_url": f"https://github.com/owner/demo/pull/{20 + i}",
    "state": "open", "draft": True, "title": row["plan"]["title"], "body": row["plan"]["body_draft"],
    "head": {"ref": row["plan"]["branch"], "sha": row["plan"]["head_sha"], "repo": {"full_name": "owner/demo"}},
    "base": {"ref": row["plan"]["stack"]["base_branch"], "repo": {"full_name": "owner/demo"}},
  } for i, row in enumerate(records)}
  by_number = {value["number"]: value for value in lives.values()}
  def remote(repo, *args, **kwargs):
    assert args[0] == "api" and len(args) == 2
    f["calls"].append(args[1])
    target = args[1]
    if "/git/ref/heads/" in target:
      ref = unquote(target.split("/git/ref/heads/", 1)[1])
      sha = lives[ref]["head"]["sha"] if ref in lives else "b" * 40
      return subprocess.CompletedProcess(args, 0, json.dumps({"object": {"sha": sha}}), "")
    if "/pulls?" in target:
      ref = unquote(target.split("head=", 1)[1].split("&", 1)[0]).split(":", 1)[1]
      return subprocess.CompletedProcess(args, 0, json.dumps([lives[ref]]), "")
    return subprocess.CompletedProcess(args, 0, json.dumps(by_number[int(target.rsplit("/", 1)[1])]), "")
  monkeypatch.setattr(github_contribution_git, "_gh", remote)
  return {**f, "records": records, "ids": ids,
          "url": f"/api/github/contributions/{f['app_id']}/layer-1/observe"}


def test_stack_member_settles_alone_and_never_replays_a_sibling(client, stack, monkeypatch):
  f = stack
  url = lambda record_id: f"/api/github/contributions/{f['app_id']}/{record_id}/observe"
  first, second = f["ids"]
  _crash_after_journal(client, f, monkeypatch, url(first))
  path = contribution_runtime.personal_attempt_path(f["app_id"], first)
  journal = json.loads(path.read_text())["observation_settlement"]
  assert journal["before"]["id"] == journal["after"]["id"] == first
  sibling = client.post(url(second), headers=f["headers"]).json()
  assert sibling["outcome"] == "settled", sibling
  assert [row["id"] for row in sibling["records"]] == [second]
  sibling_path = routes._record_paths(f["app_id"], second)[0]
  sibling_bytes = sibling_path.read_bytes()
  monkeypatch.setattr(routes, "observe_publication_effect", lambda *a: pytest.fail("local replay must not reread remote"))
  recovered = client.post(url(first), headers=f["headers"]).json()
  assert recovered["outcome"] == "settled", recovered
  assert [row["id"] for row in recovered["records"]] == [first]
  assert sibling_path.read_bytes() == sibling_bytes
  assert routes._read_record(routes._record_paths(f["app_id"], first)[0])["status"] == "draft"
  assert not path.exists()


@pytest.mark.parametrize('damage', ['benign', 'head', 'partial', 'unavailable', 'replacement', 'race'])
def test_unapplied_journal_reproves_same_attempt_and_preserves_local_edits(client, observation, monkeypatch, damage):
  f = observation
  _crash_after_journal(client, f, monkeypatch)
  receipt_before = receipt_path(f).read_bytes()
  row = routes._read_record(f['path'])
  row.update(local_owner_note='retain note', updated_at='later',
             stop_requested=True, questions=[{'text': 'Keep question'}])
  if damage == 'head': row['plan'] = {**row['plan'], 'head_sha': 'e' * 40}
  if damage == 'partial': row['number'] = 17
  routes._write_record(f['path'], row)
  original = routes.observe_publication_effect
  calls = []
  def proof(record, attempt):
    calls.append(True)
    if damage == 'unavailable': return {'outcome':'unavailable','message':'Unavailable'}
    result = original(record, attempt)
    if damage == 'replacement':
      replacement = {**attempt, 'updated_at':'replacement-generation'}
      receipt_path(f).write_text(json.dumps(routes._signed_personal_attempt(replacement)))
    if damage == 'race':
      latest = routes._read_record(f['path']); latest['another_note']='race'
      routes._write_record(f['path'], latest)
    return result
  monkeypatch.setattr(routes, 'observe_publication_effect', proof)
  result = client.post(f['url'], headers=f['headers']).json()
  saved = routes._read_record(f['path'])
  assert saved['local_owner_note'] == 'retain note' and saved['stop_requested']
  assert saved['questions'] == [{'text':'Keep question'}]
  if damage == 'benign':
    assert calls and result['outcome']=='settled', result
    assert saved['status']=='draft' and not receipt_path(f).exists()
  else:
    assert result['outcome'] == ('unavailable' if damage=='unavailable' else 'changed'), result
    assert saved['status']=='prepared' and receipt_path(f).exists()
    if damage != 'replacement': assert receipt_path(f).read_bytes()==receipt_before
    if damage in {'head','partial'}: assert not calls

