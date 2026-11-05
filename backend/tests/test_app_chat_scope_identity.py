"""A scoped app start admits at most one first turn per app and scope.

Each case stands for a real recovery path: a lost response replayed after the
owner deleted or recovered the chat, a cancellation that must outrun a delayed
original request, a retry racing a timed-out request's still-queued command,
the chat's hard purge, a restart, and two panes racing. The
provider is never launched: first-turn admission is stubbed at
``chats_stream.send_message``, the boundary the route crosses.
"""
import asyncio
from datetime import timedelta

import pytest
from fastapi.responses import JSONResponse
from sqlalchemy.orm import object_session

from app import models, transcript_rows
from app.timeutil import now_naive_utc
from test_app_fixtures import create_local_app


def _make_app(client, owner_token, name):
  owner = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner, name=name)["id"]
  token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=owner,
  ).json()["token"]
  return app_id, token


def _accept_first_turns(monkeypatch, *, admitted=None, gate=None):
  """Admit each first turn as the real send path would, without a provider."""
  from app.routes import chats_stream

  async def accept(body, chat_id, principal, request_db):
    if gate is not None:
      gate["entered"].set()
      await gate["release"].wait()
    row = request_db.query(models.Chat).filter(models.Chat.id == chat_id).one()
    transcript_rows.replace_all(
      object_session(row), row,
      [{"role": "user", "content": body.content, "cid": body.cid}],
    )
    row.has_messages = True
    request_db.commit()
    if admitted is not None:
      admitted.append(chat_id)
    return JSONResponse({"status": "started"}, status_code=202)

  monkeypatch.setattr(chats_stream, "send_message", accept)


def _start(client, token, scope, **extra):
  return client.post("/api/app-chats/start", json={
    "title": "Prepare project",
    "scope": scope,
    "owner_visible": True,
    "content": "frozen first-turn prompt",
    **extra,
  }, headers={"Authorization": f"Bearer {token}"})


def _cancel(client, token, scope):
  return client.post(
    "/api/app-chats/start/cancel", json={"scope": scope},
    headers={"Authorization": f"Bearer {token}"},
  )


def _app_chats(db, app_id):
  db.expire_all()
  return db.query(models.Chat).filter(models.Chat.created_by_app_id == app_id).all()


def test_owner_deleting_the_chat_releases_the_scope_for_a_fresh_chat(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "deleted-scope")
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)
  first = _start(client, token, "cycle:lost")
  assert first.status_code == 200, first.text
  chat_id = first.json()["chat_id"]

  # The app never saw that response; the owner deletes the chat it created.
  deleted = client.delete(
    f"/api/chats/{chat_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert deleted.status_code == 204, deleted.text
  replay = _start(client, token, "cycle:lost", cid="recovery")
  again = _start(client, token, "cycle:lost", cid="again")

  assert replay.status_code == 200, replay.text
  fresh = replay.json()["chat_id"]
  assert fresh != chat_id and replay.json()["outcome"] == "started"
  assert again.json() == {"chat_id": fresh, "outcome": "reused", "response": None}
  assert admitted == [chat_id, fresh]
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:lost")).chat_id == fresh


def test_owner_recovery_restores_the_same_scoped_chat_for_replay(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "recovered-scope")
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)
  chat_id = _start(client, token, "cycle:recover").json()["chat_id"]
  owner = {"Authorization": f"Bearer {owner_token}"}
  assert client.delete(f"/api/chats/{chat_id}", headers=owner).status_code == 204
  assert client.post(f"/api/chats/{chat_id}/recover", headers=owner).status_code == 200

  replay = _start(client, token, "cycle:recover", cid="after-recovery")

  assert replay.status_code == 200, replay.text
  assert replay.json() == {"chat_id": chat_id, "outcome": "reused", "response": None}
  assert admitted == [chat_id]
  assert len(_app_chats(db, app_id)) == 1


def test_hard_purged_scoped_chat_leaves_its_scope_free_for_a_fresh_chat(
  client, owner_token, db, monkeypatch,
):
  from app.chat_retention import purge_expired_chat_tombstones

  app_id, token = _make_app(client, owner_token, "purged-scope")
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)
  chat_id = _start(client, token, "cycle:purged").json()["chat_id"]
  row = db.get(models.Chat, chat_id)
  row.deleted_at = now_naive_utc() - timedelta(days=8)
  db.commit()
  assert chat_id in purge_expired_chat_tombstones(db)
  assert db.get(models.Chat, chat_id) is None

  replay = _start(client, token, "cycle:purged", cid="after-purge")

  assert replay.status_code == 200, replay.text
  fresh = replay.json()["chat_id"]
  assert fresh != chat_id and replay.json()["outcome"] == "started"
  assert admitted == [chat_id, fresh]
  assert [chat.id for chat in _app_chats(db, app_id)] == [fresh]


def test_cancel_before_a_delayed_start_fences_the_scope_forever(
  client, owner_token, db, monkeypatch,
):
  from app import chat_queue

  app_id, token = _make_app(client, owner_token, "cancelled-scope")
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)

  cancelled = _cancel(client, token, "cycle:unreceived")
  repeated = _cancel(client, token, "cycle:unreceived")
  # In-memory lock state is not the fence: a restart forgets it entirely.
  chat_queue.reset_for_tests()
  delayed = _start(client, token, "cycle:unreceived")

  assert cancelled.status_code == 200, cancelled.text
  assert cancelled.json() == {
    "scope": "cycle:unreceived", "outcome": "cancelled", "chat_id": None,
  }
  assert repeated.json() == cancelled.json()
  assert delayed.status_code == 409, delayed.text
  assert delayed.json()["detail"]["code"] == "scope_cancelled"
  assert delayed.json()["detail"]["chat_id"] is None
  assert admitted == []
  assert _app_chats(db, app_id) == []


def test_cancel_after_admission_reports_the_started_chat_and_never_stops_it(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "admitted-scope")
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)
  chat_id = _start(client, token, "cycle:admitted").json()["chat_id"]

  cancelled = _cancel(client, token, "cycle:admitted")
  replay = _start(client, token, "cycle:admitted", cid="replay")

  assert cancelled.status_code == 200, cancelled.text
  assert cancelled.json() == {
    "scope": "cycle:admitted", "outcome": "started", "chat_id": chat_id,
  }
  assert replay.json() == {"chat_id": chat_id, "outcome": "reused", "response": None}
  assert admitted == [chat_id]
  row = _app_chats(db, app_id)[0]
  assert row.deleted_at is None and row.has_messages


def test_cancel_fences_the_in_place_retry_of_an_unadmitted_chat(
  client, owner_token, db, monkeypatch,
):
  from app.routes import chats_stream

  app_id, token = _make_app(client, owner_token, "pending-scope")

  async def reject(body, chat_id, principal, request_db):
    return JSONResponse({"detail": "busy"}, status_code=409)

  monkeypatch.setattr(chats_stream, "send_message", reject)
  rejected = _start(client, token, "cycle:pending")
  assert rejected.status_code == 502, rejected.text
  chat_id = rejected.json()["detail"]["chat_id"]
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)

  cancelled = _cancel(client, token, "cycle:pending")
  retry = _start(client, token, "cycle:pending", cid="retry")

  assert cancelled.json() == {
    "scope": "cycle:pending", "outcome": "cancelled", "chat_id": chat_id,
  }
  assert retry.status_code == 409, retry.text
  assert retry.json()["detail"] == {
    "code": "scope_cancelled",
    "chat_id": chat_id,
    "message": retry.json()["detail"]["message"],
  }
  assert admitted == []
  # The owner's empty chat is left for the owner; nothing is erased.
  assert [chat.id for chat in _app_chats(db, app_id)] == [chat_id]


def test_cancel_after_deletion_before_the_app_linked_fences_the_released_scope(
  client, owner_token, db, monkeypatch,
):
  _, token = _make_app(client, owner_token, "deleted-before-link")
  _accept_first_turns(monkeypatch)
  chat_id = _start(client, token, "cycle:unlinked").json()["chat_id"]
  assert client.delete(
    f"/api/chats/{chat_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  ).status_code == 204

  cancelled = _cancel(client, token, "cycle:unlinked")
  replay = _start(client, token, "cycle:unlinked", cid="replay")

  # Deletion already stopped that chat's work, so nothing of the scope runs.
  assert cancelled.json() == {
    "scope": "cycle:unlinked", "outcome": "cancelled", "chat_id": None,
  }
  assert replay.status_code == 409, replay.text
  assert replay.json()["detail"]["code"] == "scope_cancelled"


def test_cancel_waits_for_an_in_flight_admission_and_reports_it(
  client, owner_token, db, monkeypatch,
):
  from app.database import SessionLocal
  from app.deps import Principal
  from app.routes.chats import (
    AppChatStart, AppChatStartCancel, cancel_scoped_app_chat_start,
    start_scoped_app_chat,
  )

  app_id, _ = _make_app(client, owner_token, "racing-scope")

  async def race():
    gate = {"entered": asyncio.Event(), "release": asyncio.Event()}
    admitted = []
    _accept_first_turns(monkeypatch, admitted=admitted, gate=gate)
    start_db, cancel_db = SessionLocal(), SessionLocal()
    try:
      principal = Principal(
        owner=start_db.query(models.Owner).one(), app_id=app_id, scope="app",
      )
      start = asyncio.create_task(start_scoped_app_chat(
        AppChatStart(scope="cycle:race", content="frozen prompt"),
        principal, start_db,
      ))
      await gate["entered"].wait()
      cancel = asyncio.create_task(cancel_scoped_app_chat_start(
        AppChatStartCancel(scope="cycle:race"), principal, cancel_db,
      ))
      await asyncio.sleep(0.05)
      held_back = not cancel.done()
      gate["release"].set()
      return held_back, await start, await cancel, admitted
    finally:
      start_db.close()
      cancel_db.close()

  held_back, started, cancelled, admitted = asyncio.run(race())

  assert held_back
  assert started["outcome"] == "started"
  assert cancelled == {
    "scope": "cycle:race", "outcome": "started", "chat_id": started["chat_id"],
  }
  assert admitted == [started["chat_id"]]
  assert len(_app_chats(db, app_id)) == 1


def test_purged_app_takes_its_scope_identities_so_a_reused_id_starts_clean(
  client, owner_token, db, monkeypatch,
):
  from app.routes import apps as apps_route

  app_id, token = _make_app(client, owner_token, "purged-app-scope")
  _accept_first_turns(monkeypatch)
  assert _start(client, token, "cycle:old-app").status_code == 200
  assert _cancel(client, token, "cycle:fenced").json()["outcome"] == "cancelled"
  row = db.get(models.App, app_id)
  row.deleted_at = now_naive_utc() - apps_route.APP_SOFT_DELETE_TTL - timedelta(seconds=1)
  db.commit()

  listed = client.get("/api/apps/", headers={"Authorization": f"Bearer {owner_token}"})

  assert listed.status_code == 200, listed.text
  db.expire_all()
  assert db.get(models.App, app_id) is None
  assert db.query(models.AppChatScopeStart).filter(
    models.AppChatScopeStart.app_id == app_id,
  ).count() == 0


def test_scope_identity_belongs_to_one_app(
  client, owner_token, db, monkeypatch,
):
  first_app, first_token = _make_app(client, owner_token, "scope-owner")
  second_app, second_token = _make_app(client, owner_token, "scope-neighbour")
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)

  assert _cancel(client, first_token, "cycle:shared").json()["outcome"] == "cancelled"
  neighbour = _start(client, second_token, "cycle:shared")
  owner_cancel = client.post(
    "/api/app-chats/start/cancel", json={"scope": "cycle:shared"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert neighbour.status_code == 200, neighbour.text
  assert neighbour.json()["outcome"] == "started"
  assert _cancel(client, second_token, "cycle:shared").json()["outcome"] == "started"
  assert owner_cancel.status_code == 403
  assert len(_app_chats(db, second_app)) == 1
  assert db.get(models.AppChatScopeStart, (first_app, "cycle:shared")).cancelled_at


def test_unbound_scoped_chat_from_an_earlier_release_is_reused_and_bound(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "legacy-scope")
  created = client.post("/api/app-chats", json={
    "title": "Legacy", "scope": "cycle:legacy",
    "provider": "claude", "model": "claude-opus-4-8",
  }, headers={"Authorization": f"Bearer {token}"})
  assert created.status_code == 201, created.text
  legacy_id = created.json()["id"]
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:legacy")) is None
  admitted = []
  _accept_first_turns(monkeypatch, admitted=admitted)

  started = _start(client, token, "cycle:legacy")
  db.expire_all()

  assert started.json() == {"chat_id": legacy_id, "outcome": "started", "response": None}
  assert admitted == [legacy_id]
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:legacy")).chat_id == legacy_id


def _queue_first_turns_through_the_real_send_path(monkeypatch):
  """Run the owning send path with no provider: a restart drain queues it."""
  from app.routes import chats_stream

  monkeypatch.setattr(chats_stream, "is_draining", lambda: True)


def _pending_cids(db, chat_id):
  db.expire_all()
  return [row.get("cid") for row in db.get(models.Chat, chat_id).pending_messages or []]


def test_owner_stop_of_a_queued_first_turn_never_lets_a_replay_admit_it_again(
  client, owner_token, db, monkeypatch,
):
  from app import chat_queue

  app_id, token = _make_app(client, owner_token, "queued-stop-scope")
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  first = _start(client, token, "cycle:queued-stop", cid="first-cid")
  assert first.status_code == 200, first.text
  chat_id = first.json()["chat_id"]
  assert first.json()["outcome"] == "started"
  assert _pending_cids(db, chat_id) == ["first-cid"]

  stop = client.post(
    "/api/chat/stop", json={"chat_id": chat_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert stop.status_code == 200, stop.text
  assert stop.json()["cleared_pending_cids"] == ["first-cid"]
  assert stop.json()["stop_receipt"] == {"chat_id": chat_id, "run_id": None}
  assert _pending_cids(db, chat_id) == []
  chat_queue.reset_for_tests()  # A restart forgets every in-memory lock.

  replay = _start(client, token, "cycle:queued-stop", cid="replay-cid")
  cancelled = _cancel(client, token, "cycle:queued-stop")

  assert replay.json() == {"chat_id": chat_id, "outcome": "reused", "response": None}
  assert _pending_cids(db, chat_id) == []
  assert cancelled.json() == {
    "scope": "cycle:queued-stop", "outcome": "started", "chat_id": chat_id,
  }
  assert [chat.id for chat in _app_chats(db, app_id)] == [chat_id]


def test_rejected_first_turn_is_retried_once_in_the_same_chat_through_the_real_send_path(
  client, owner_token, db, monkeypatch,
):
  from app.routes import chats_stream

  app_id, token = _make_app(client, owner_token, "rejected-then-queued-scope")
  real_send = chats_stream.send_message

  async def reject(body, chat_id, principal, request_db):
    return JSONResponse({"detail": "busy"}, status_code=409)

  monkeypatch.setattr(chats_stream, "send_message", reject)
  rejected = _start(client, token, "cycle:retry")
  assert rejected.status_code == 502, rejected.text
  chat_id = rejected.json()["detail"]["chat_id"]
  db.expire_all()
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:retry")).admitted_at is None

  monkeypatch.setattr(chats_stream, "send_message", real_send)
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  retried = _start(client, token, "cycle:retry", cid="retry-cid")
  again = _start(client, token, "cycle:retry", cid="again-cid")

  assert retried.json() == {"chat_id": chat_id, "outcome": "started", "response": None}
  assert again.json()["outcome"] == "reused"
  assert _pending_cids(db, chat_id) == ["retry-cid"]
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:retry")).admitted_at is not None


def test_a_started_scope_seen_before_admission_was_recorded_stays_started(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "ratchet-scope")
  created = client.post("/api/app-chats", json={
    "title": "Legacy", "scope": "cycle:ratchet",
    "provider": "claude", "model": "claude-opus-4-8",
  }, headers={"Authorization": f"Bearer {token}"})
  legacy_id = created.json()["id"]
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  sent = client.post(
    f"/api/chats/{legacy_id}/messages", json={"content": "legacy turn", "cid": "legacy-cid"},
    headers={"Authorization": f"Bearer {token}"},
  )
  assert sent.status_code == 202, sent.text

  first_sight = _start(client, token, "cycle:ratchet", cid="first-sight")
  client.delete(
    f"/api/chats/{legacy_id}/pending/legacy-cid",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  replay = _start(client, token, "cycle:ratchet", cid="replay")

  assert first_sight.json()["outcome"] == "reused"
  assert replay.json() == {"chat_id": legacy_id, "outcome": "reused", "response": None}
  assert _pending_cids(db, legacy_id) == []
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:ratchet")).admitted_at is not None


@pytest.mark.parametrize("owner_action", ["stop", "remove_queued"])
def test_process_death_after_acceptance_before_the_route_returns_never_admits_twice(
  client, owner_token, db, monkeypatch, owner_action,
):
  """Acceptance and admission commit together, so no later step can lose it."""
  from app import chat_queue
  from app.routes import chats_stream

  app_id, token = _make_app(client, owner_token, f"crash-{owner_action}-scope")
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  real_send = chats_stream.send_message

  async def accepted_then_process_dies(body, chat_id, principal, request_db):
    await real_send(body, chat_id, principal, request_db)
    raise RuntimeError("process died after the first turn was accepted")

  monkeypatch.setattr(chats_stream, "send_message", accepted_then_process_dies)
  lost = _start(client, token, "cycle:crash-accepted", cid="first-cid")
  assert lost.status_code == 503, lost.text
  chat_id = lost.json()["detail"]["chat_id"]
  assert _pending_cids(db, chat_id) == ["first-cid"]
  monkeypatch.setattr(chats_stream, "send_message", real_send)

  owner = {"Authorization": f"Bearer {owner_token}"}
  if owner_action == "stop":
    cleared = client.post("/api/chat/stop", json={"chat_id": chat_id}, headers=owner)
    assert cleared.json()["cleared_pending_cids"] == ["first-cid"]
  else:
    cleared = client.delete(f"/api/chats/{chat_id}/pending/first-cid", headers=owner)
    assert cleared.status_code < 300, cleared.text
  assert _pending_cids(db, chat_id) == []
  chat_queue.reset_for_tests()

  replay = _start(client, token, "cycle:crash-accepted", cid="replay-cid")
  cancelled = _cancel(client, token, "cycle:crash-accepted")

  assert replay.json() == {"chat_id": chat_id, "outcome": "reused", "response": None}
  assert cancelled.json()["outcome"] == "started"
  assert _pending_cids(db, chat_id) == []
  assert [chat.id for chat in _app_chats(db, app_id)] == [chat_id]


def _grouped_chat_with_a_turn(client, token, scope, title, cid):
  """A chat grouped through POST /api/app-chats, sent outside /start."""
  app_auth = {"Authorization": f"Bearer {token}"}
  created = client.post("/api/app-chats", json={
    "title": title, "scope": scope, "owner_visible": True,
  }, headers=app_auth)
  assert created.status_code == 201, created.text
  chat_id = created.json()["id"]
  sent = client.post(
    f"/api/chats/{chat_id}/messages", json={"content": title, "cid": cid},
    headers=app_auth,
  )
  assert sent.status_code == 202, sent.text
  return chat_id


def _rejected_start(client, token, monkeypatch, scope):
  """Bind a scope to an empty chat whose first turn was not accepted."""
  from app.routes import chats_stream

  async def reject(body, chat_id, principal, request_db):
    return JSONResponse({"detail": "busy"}, status_code=409)

  with monkeypatch.context() as patch:
    patch.setattr(chats_stream, "send_message", reject)
    rejected = _start(client, token, scope)
  assert rejected.status_code == 502, rejected.text
  return rejected.json()["detail"]["chat_id"]


def test_grouped_chats_sent_outside_start_keep_newest_live_reuse(
  client, owner_token, db, monkeypatch,
):
  """Grouping is not a /start job: it never claims the scope's identity."""
  app_id, token = _make_app(client, owner_token, "grouped-newest-scope")
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  older = _grouped_chat_with_a_turn(client, token, "g:mixed", "Older", "older-cid")
  newer = _grouped_chat_with_a_turn(client, token, "g:mixed", "Newer", "newer-cid")
  assert db.get(models.AppChatScopeStart, (app_id, "g:mixed")) is None

  started = _start(client, token, "g:mixed", cid="start-cid")

  assert started.json() == {"chat_id": newer, "outcome": "reused", "response": None}
  assert _pending_cids(db, older) == ["older-cid"]
  assert _pending_cids(db, newer) == ["newer-cid"]


def test_deleting_one_grouped_chat_leaves_start_on_its_live_sibling(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "grouped-sibling-scope")
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  first = _grouped_chat_with_a_turn(client, token, "g:sib", "First", "first-cid")
  sibling = _grouped_chat_with_a_turn(client, token, "g:sib", "Sibling", "sibling-cid")
  owner = {"Authorization": f"Bearer {owner_token}"}
  assert client.delete(f"/api/chats/{first}", headers=owner).status_code == 204

  started = _start(client, token, "g:sib", cid="start-cid")

  assert started.status_code == 200, started.text
  assert started.json() == {"chat_id": sibling, "outcome": "reused", "response": None}
  assert _pending_cids(db, sibling) == ["sibling-cid"]


def test_a_grouped_sibling_cannot_admit_a_still_pending_bound_scope(
  client, owner_token, db, monkeypatch,
):
  app_id, token = _make_app(client, owner_token, "sibling-stamp-scope")
  original = _rejected_start(client, token, monkeypatch, "g:pending")
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  _grouped_chat_with_a_turn(client, token, "g:pending", "Sibling", "sibling-cid")
  db.expire_all()
  binding = db.get(models.AppChatScopeStart, (app_id, "g:pending"))
  assert binding.chat_id == original and binding.admitted_at is None

  retried = _start(client, token, "g:pending", cid="retry-cid")

  assert retried.json() == {"chat_id": original, "outcome": "started", "response": None}
  assert _pending_cids(db, original) == ["retry-cid"]
  db.expire_all()
  assert db.get(models.AppChatScopeStart, (app_id, "g:pending")).admitted_at is not None


def test_a_failed_acceptance_commit_rolls_back_the_queue_and_the_admission(
  client, owner_token, db, monkeypatch,
):
  from app import chat_writer

  app_id, token = _make_app(client, owner_token, "rollback-scope")
  original = _rejected_start(client, token, monkeypatch, "g:rollback")
  _queue_first_turns_through_the_real_send_path(monkeypatch)
  real_commit = chat_writer._commit_or_rollback
  failed = []

  def commit_fails_once(session):
    if not failed and session.query(models.AppChatScopeStart).filter(
      models.AppChatScopeStart.admitted_at.isnot(None),
    ).count():
      failed.append(True)
      session.rollback()
      return False
    return real_commit(session)

  monkeypatch.setattr(chat_writer, "_commit_or_rollback", commit_fails_once)
  retried = _start(client, token, "g:rollback", cid="lost-cid")

  assert failed == [True]
  assert retried.status_code >= 500, retried.text
  assert _pending_cids(db, original) == []
  db.expire_all()
  assert db.get(models.AppChatScopeStart, (app_id, "g:rollback")).admitted_at is None


def test_writer_admits_a_bound_scope_in_the_same_commit_as_its_first_turn(
  client, owner_token, db, monkeypatch,
):
  from app.chat_writer import StartTurn, StartTurnBlockedByPendingQuestion, get_writer
  from app.routes import chats_stream

  app_id, token = _make_app(client, owner_token, "writer-admission-scope")

  async def reject(body, chat_id, principal, request_db):
    return JSONResponse({"detail": "busy"}, status_code=409)

  monkeypatch.setattr(chats_stream, "send_message", reject)
  chat_id = _start(client, token, "cycle:writer").json()["detail"]["chat_id"]

  def first_turn(token_id):
    return get_writer().submit(StartTurn(
      chat_id=chat_id, run_token=token_id,
      user_msg={"role": "user", "content": "go", "cid": f"{token_id}-cid", "ts": 1},
    )).result(timeout=5)

  def admitted():
    db.expire_all()
    return db.get(models.AppChatScopeStart, (app_id, "cycle:writer")).admitted_at

  db.get(models.Chat, chat_id).pending_question_id = "open-question"
  db.commit()
  assert isinstance(first_turn("blocked-run"), StartTurnBlockedByPendingQuestion)
  assert admitted() is None
  db.get(models.Chat, chat_id).pending_question_id = None
  db.commit()

  first_turn("first-run")

  assert admitted() is not None
  assert db.get(models.ChatRun, "first-run").status == "running"


def test_cancel_never_fences_a_turn_the_writer_admitted_after_classification(
  client, owner_token, db, monkeypatch,
):
  from app.chat_writer import AppendPending, get_writer
  from app.routes import chats, chats_stream

  app_id, token = _make_app(client, owner_token, "cancel-race-scope")

  async def reject(body, chat_id, principal, request_db):
    return JSONResponse({"detail": "busy"}, status_code=409)

  monkeypatch.setattr(chats_stream, "send_message", reject)
  chat_id = _start(client, token, "cycle:cancel-race").json()["detail"]["chat_id"]
  real_state = chats._resolve_scoped_start
  calls = []

  def owner_turn_lands_after_classification(session, app, scope):
    result = real_state(session, app, scope)
    if not calls:
      # The owner's own message into the empty chat commits between the
      # cancel's read and its fence; the writer does not take the scope lock.
      get_writer().submit(AppendPending(
        chat_id=chat_id, owner_input=True,
        user_msg={"role": "user", "content": "mine", "cid": "owner-cid", "ts": 1},
      )).result(timeout=5)
    calls.append(result[0])
    return result

  monkeypatch.setattr(chats, "_resolve_scoped_start", owner_turn_lands_after_classification)
  cancelled = _cancel(client, token, "cycle:cancel-race")

  assert calls[0] == "pending"
  assert cancelled.status_code == 200, cancelled.text
  assert cancelled.json() == {
    "scope": "cycle:cancel-race", "outcome": "started", "chat_id": chat_id,
  }
  db.expire_all()
  binding = db.get(models.AppChatScopeStart, (app_id, "cycle:cancel-race"))
  assert binding.cancelled_at is None and binding.admitted_at is not None


def _hold_the_scoped_writer_command(monkeypatch, *, run_held_before_next=False):
  """Let /start's writer ack time out while its command stays undelivered.

  This is the writer paused after dequeue: the request gives up (503) and
  releases the scope lock, and the exact command it issued runs later. With
  ``run_held_before_next`` the writer resumes just as a later scoped command
  arrives, so it runs the held command first, as its FIFO queue would.
  """
  from concurrent.futures import Future

  from app import chat_writer
  from app.routes import chats_stream

  held = []
  writer = chat_writer.get_writer()

  class Paused:
    def submit(self, cmd):
      if getattr(cmd, "scoped_start", None) is not None:
        if held and run_held_before_next:
          monkeypatch.setattr(chat_writer, "ACK_TIMEOUT_SECS", 30.0)
          writer.submit(held[0]).result(timeout=5)
          return writer.submit(cmd)
        held.append(cmd)
        return Future()  # Never acknowledged inside the request.
      return writer.submit(cmd)

  monkeypatch.setattr(chat_writer, "ACK_TIMEOUT_SECS", 0.05)
  monkeypatch.setattr(chats_stream, "get_writer", lambda: Paused())
  return held


@pytest.mark.parametrize("command", ["StartTurn", "AppendPending"])
def test_cancel_after_a_writer_ack_timeout_fences_the_still_dispatched_first_turn(
  client, owner_token, db, monkeypatch, command, caplog,
):
  from app.app_chat_scopes import ScopedStartRefused
  from app.chat_writer import get_writer

  app_id, token = _make_app(client, owner_token, f"timeout-cancel-{command}")
  if command == "AppendPending":
    _queue_first_turns_through_the_real_send_path(monkeypatch)
  held = _hold_the_scoped_writer_command(monkeypatch)

  timed_out = _start(client, token, "cycle:timeout", cid="late-cid")
  assert timed_out.status_code == 503, timed_out.text
  chat_id = held[0].chat_id
  assert [type(cmd).__name__ for cmd in held] == [command]
  assert held[0].scoped_start == (app_id, "cycle:timeout")

  cancelled = _cancel(client, token, "cycle:timeout")
  assert cancelled.json() == {
    "scope": "cycle:timeout", "outcome": "cancelled", "chat_id": chat_id,
  }

  # The writer now runs the exact command /start issued: it must commit nothing.
  with pytest.raises(ScopedStartRefused) as refused:
    get_writer().submit(held[0]).result(timeout=5)
  assert refused.value.admitted is False
  # An expected refusal, not a writer failure with a traceback.
  assert not [r for r in caplog.records if "command failed" in r.getMessage()]
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  assert transcript_rows.read_all(db, chat) == []
  assert _pending_cids(db, chat_id) == []
  assert db.query(models.ChatRun).filter(models.ChatRun.chat_id == chat_id).count() == 0
  binding = db.get(models.AppChatScopeStart, (app_id, "cycle:timeout"))
  assert binding.cancelled_at is not None and binding.admitted_at is None

  # An ordinary send to that chat carries no scoped identity and is unaffected.
  from app.chat_writer import AppendPending
  get_writer().submit(AppendPending(
    chat_id=chat_id, owner_input=True,
    user_msg={"role": "user", "content": "mine", "cid": "owner-cid", "ts": 1},
  )).result(timeout=5)
  assert _pending_cids(db, chat_id) == ["owner-cid"]
  db.expire_all()
  assert db.get(models.AppChatScopeStart, (app_id, "cycle:timeout")).admitted_at is None


@pytest.mark.parametrize("command", ["StartTurn", "AppendPending"])
def test_a_scoped_command_the_writer_runs_after_a_timeout_admits_once_before_cancel(
  client, owner_token, db, monkeypatch, command,
):
  from app.chat_writer import get_writer

  app_id, token = _make_app(client, owner_token, f"timeout-admit-{command}")
  if command == "AppendPending":
    _queue_first_turns_through_the_real_send_path(monkeypatch)
  held = _hold_the_scoped_writer_command(monkeypatch)
  assert _start(client, token, "cycle:late").status_code == 503
  chat_id = held[0].chat_id

  get_writer().submit(held[0]).result(timeout=5)
  cancelled = _cancel(client, token, "cycle:late")
  replay = _start(client, token, "cycle:late", cid="replay-cid")

  assert cancelled.json() == {
    "scope": "cycle:late", "outcome": "started", "chat_id": chat_id,
  }
  assert replay.json() == {"chat_id": chat_id, "outcome": "reused", "response": None}
  assert len(held) == 1


def test_start_maps_a_writer_side_cancellation_to_scope_cancelled(
  client, owner_token, db, monkeypatch,
):
  from app.app_chat_scopes import ScopedStartRefused
  from app.routes import chats_stream

  app_id, token = _make_app(client, owner_token, "writer-refusal-scope")

  async def cancel_wins_inside_the_writer(body, chat_id, principal, request_db):
    assert body._scoped_start == (app_id, "cycle:refused")
    raise ScopedStartRefused(chat_id, admitted=False)

  monkeypatch.setattr(chats_stream, "send_message", cancel_wins_inside_the_writer)
  refused = _start(client, token, "cycle:refused")

  assert refused.status_code == 409, refused.text
  assert refused.json()["detail"]["code"] == "scope_cancelled"


@pytest.mark.parametrize("command", ["StartTurn", "AppendPending"])
def test_a_retry_behind_a_timed_out_start_reuses_its_first_turn_instead_of_a_second(
  client, owner_token, db, monkeypatch, command,
):
  """Regression: the writer must refuse a second /start command for a scope
  whose first turn an earlier, timed-out request's command already admitted."""
  app_id, token = _make_app(client, owner_token, f"timeout-retry-{command}")
  if command == "AppendPending":
    _queue_first_turns_through_the_real_send_path(monkeypatch)
  held = _hold_the_scoped_writer_command(monkeypatch, run_held_before_next=True)
  timed_out = _start(client, token, "cycle:retry", cid="late-cid")
  assert timed_out.status_code == 503, timed_out.text
  chat_id = held[0].chat_id

  retry = _start(client, token, "cycle:retry", cid="retry-cid")

  assert retry.status_code == 200, retry.text
  assert retry.json() == {"chat_id": chat_id, "outcome": "reused", "response": None}
  db.expire_all()
  chat = db.get(models.Chat, chat_id)
  accepted = [row.get("cid") for row in transcript_rows.read_all(db, chat)]
  assert accepted + _pending_cids(db, chat_id) == ["late-cid"]
  binding = db.get(models.AppChatScopeStart, (app_id, "cycle:retry"))
  assert binding.admitted_at is not None and binding.cancelled_at is None
