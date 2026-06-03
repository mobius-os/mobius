"""Agent self-scheduling: helper (enqueue/list_due/cap/cancel) + endpoint."""

import json
from pathlib import Path

import pytest

from app import self_reminders
from app.config import get_settings


def _store_lines() -> list[dict]:
  path = Path(get_settings().data_dir) / "shared" / "self-reminders.jsonl"
  if not path.exists():
    return []
  return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


# --- Helper: enqueue ---------------------------------------------------


def test_enqueue_writes_pending_record():
  rec = self_reminders.enqueue("chatA", "follow up", due_in_seconds=3600,
                               now=1000)
  assert rec["chat_id"] == "chatA"
  assert rec["note"] == "follow up"
  assert rec["status"] == "pending"
  assert rec["due_at"] == 1000 + 3600
  assert rec["created_at"] == 1000
  assert rec["id"]
  lines = _store_lines()
  assert len(lines) == 1
  assert lines[0]["status"] == "pending"


def test_enqueue_accepts_absolute_due_at():
  rec = self_reminders.enqueue("chatA", "x", due_at=9999, now=1000)
  assert rec["due_at"] == 9999


def test_enqueue_requires_exactly_one_due_field():
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", "n", now=1000)  # neither
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", "n", due_at=2000, due_in_seconds=60,
                           now=1000)  # both


def test_enqueue_rejects_past_and_nonpositive_due():
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", "n", due_at=500, now=1000)  # past
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", "n", due_in_seconds=0, now=1000)


def test_enqueue_rejects_beyond_horizon():
  too_far = self_reminders.MAX_HORIZON_SECONDS + 1
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", "n", due_in_seconds=too_far, now=1000)
  # Exactly at the horizon is allowed.
  rec = self_reminders.enqueue("c", "n",
                               due_in_seconds=self_reminders.MAX_HORIZON_SECONDS,
                               now=1000)
  assert rec["status"] == "pending"


def test_enqueue_requires_chat_id_and_note():
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("", "n", due_in_seconds=60, now=1000)
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", "   ", due_in_seconds=60, now=1000)


def test_enqueue_rejects_overlong_note():
  long_note = "x" * (self_reminders.MAX_NOTE_LEN + 1)
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("c", long_note, due_in_seconds=60, now=1000)


# --- Helper: cap -------------------------------------------------------


def test_per_chat_cap_blocks_overflow_but_not_other_chats():
  for i in range(self_reminders.MAX_PENDING_PER_CHAT):
    self_reminders.enqueue("capped", f"n{i}", due_in_seconds=60 + i,
                           now=1000)
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.enqueue("capped", "one too many", due_in_seconds=99,
                           now=1000)
  # A different chat is unaffected.
  other = self_reminders.enqueue("other", "fine", due_in_seconds=60,
                                 now=1000)
  assert other["status"] == "pending"


def test_cancelling_frees_a_cap_slot():
  recs = [
    self_reminders.enqueue("c", f"n{i}", due_in_seconds=60 + i, now=1000)
    for i in range(self_reminders.MAX_PENDING_PER_CHAT)
  ]
  self_reminders.cancel(recs[0]["id"])
  # The cap now has room for one more.
  again = self_reminders.enqueue("c", "back in", due_in_seconds=999,
                                 now=1000)
  assert again["status"] == "pending"


# --- Helper: list_due --------------------------------------------------


def test_list_due_returns_only_ripe_pending_oldest_first():
  self_reminders.enqueue("c", "soon", due_at=1100, now=1000)
  self_reminders.enqueue("c", "later", due_at=5000, now=1000)
  self_reminders.enqueue("c", "earliest", due_at=1050, now=1000)
  due = self_reminders.list_due(now=2000)
  notes = [r["note"] for r in due]
  assert notes == ["earliest", "soon"]  # 5000 not yet due, sorted by due_at


def test_list_due_excludes_done_and_cancelled():
  a = self_reminders.enqueue("c", "a", due_at=1100, now=1000)
  b = self_reminders.enqueue("c", "b", due_at=1100, now=1000)
  self_reminders.mark_done(a["id"])
  self_reminders.cancel(b["id"])
  assert self_reminders.list_due(now=2000) == []


# --- Helper: mark_done / cancel transitions ----------------------------


def test_mark_done_appends_terminal_record_and_is_idempotent():
  rec = self_reminders.enqueue("c", "n", due_at=1100, now=1000)
  done = self_reminders.mark_done(rec["id"])
  assert done["status"] == "done"
  # The folded view shows the terminal status; pending list is empty.
  assert self_reminders.list_pending("c") == []
  # Re-marking a done reminder raises (dispatcher idempotency).
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.mark_done(rec["id"])


def test_cancel_unknown_id_raises():
  with pytest.raises(self_reminders.ReminderError):
    self_reminders.cancel("does-not-exist")


def test_store_is_append_only():
  rec = self_reminders.enqueue("c", "n", due_at=1100, now=1000)
  self_reminders.mark_done(rec["id"])
  lines = _store_lines()
  # Two physical lines for one logical reminder: the original + the
  # terminal record, same id.
  assert len(lines) == 2
  assert {l["status"] for l in lines} == {"pending", "done"}
  assert lines[0]["id"] == lines[1]["id"]


# --- Helper: dispatcher gate -------------------------------------------


def test_dispatcher_disabled_by_default(tmp_path):
  assert self_reminders.is_dispatcher_enabled() is False


def test_dispatcher_enabled_when_sentinel_present():
  sentinel = (
    Path(get_settings().data_dir) / "shared" / "self-reminders.enabled"
  )
  sentinel.parent.mkdir(parents=True, exist_ok=True)
  sentinel.touch()
  assert self_reminders.is_dispatcher_enabled() is True


# --- Endpoint ----------------------------------------------------------


def test_create_reminder_endpoint(client, auth, chat):
  r = client.post(
    "/api/self-reminders",
    headers=auth,
    json={"chat_id": chat.id, "note": "circle back",
          "due_in_seconds": 3600},
  )
  assert r.status_code == 201, r.text
  body = r.json()
  assert body["chat_id"] == chat.id
  assert body["status"] == "pending"
  assert body["note"] == "circle back"


def test_create_reminder_unknown_chat_404(client, auth):
  r = client.post(
    "/api/self-reminders",
    headers=auth,
    json={"chat_id": "nope", "note": "x", "due_in_seconds": 60},
  )
  assert r.status_code == 404


def test_create_reminder_bad_input_400(client, auth, chat):
  r = client.post(
    "/api/self-reminders",
    headers=auth,
    json={"chat_id": chat.id, "note": "x"},  # no due field
  )
  assert r.status_code == 400


def test_create_reminder_requires_auth(client, chat):
  r = client.post(
    "/api/self-reminders",
    json={"chat_id": chat.id, "note": "x", "due_in_seconds": 60},
  )
  assert r.status_code == 401


def test_list_reminders_endpoint(client, auth, chat):
  client.post(
    "/api/self-reminders", headers=auth,
    json={"chat_id": chat.id, "note": "a", "due_in_seconds": 60},
  )
  client.post(
    "/api/self-reminders", headers=auth,
    json={"chat_id": chat.id, "note": "b", "due_in_seconds": 120},
  )
  r = client.get(f"/api/self-reminders?chat_id={chat.id}", headers=auth)
  assert r.status_code == 200
  notes = [x["note"] for x in r.json()]
  assert notes == ["a", "b"]  # oldest-due first


def test_cancel_reminder_endpoint(client, auth, chat):
  created = client.post(
    "/api/self-reminders", headers=auth,
    json={"chat_id": chat.id, "note": "a", "due_in_seconds": 60},
  ).json()
  r = client.delete(f"/api/self-reminders/{created['id']}", headers=auth)
  assert r.status_code == 200
  assert r.json()["status"] == "cancelled"
  # Cancelling again is a 409 (already terminal).
  again = client.delete(f"/api/self-reminders/{created['id']}", headers=auth)
  assert again.status_code == 409
  # The pending list no longer shows it.
  assert client.get(
    f"/api/self-reminders?chat_id={chat.id}", headers=auth
  ).json() == []


def test_cancel_unknown_reminder_404(client, auth):
  r = client.delete("/api/self-reminders/missing", headers=auth)
  assert r.status_code == 404


def test_dispatch_is_noop_when_disabled(client, auth, chat):
  # A due reminder exists, but dispatch is off by default.
  self_reminders.enqueue(chat.id, "n", due_at=1, now=0)
  r = client.post("/api/self-reminders/dispatch", headers=auth)
  assert r.status_code == 200
  assert r.json() == {"enabled": False, "fired": 0, "reminders": []}
  # The reminder is untouched — still pending for when the owner opts in.
  assert len(self_reminders.list_pending(chat.id)) == 1


def _enable_dispatch():
  sentinel = (
    Path(get_settings().data_dir) / "shared" / "self-reminders.enabled"
  )
  sentinel.parent.mkdir(parents=True, exist_ok=True)
  sentinel.touch()


def test_dispatch_fires_due_and_marks_done(client, auth, chat, monkeypatch):
  """When enabled, dispatch resumes each due chat (via send_message) and
  marks the reminder done so it can't fire twice. send_message is patched
  so the test doesn't spawn a real agent turn."""
  _enable_dispatch()
  self_reminders.enqueue(chat.id, "due now", due_at=1, now=0)
  self_reminders.enqueue(chat.id, "future", due_in_seconds=10**6)

  posted = []

  async def _fake_send(*, body, chat_id, principal, db):
    posted.append((chat_id, body.content, body.hidden))

  monkeypatch.setattr(
    "app.routes.self_reminders._send_checkin", _fake_send,
  )
  r = client.post("/api/self-reminders/dispatch", headers=auth)
  assert r.status_code == 200
  out = r.json()
  assert out["enabled"] is True
  assert out["fired"] == 1
  # The due one was posted as a hidden message carrying the note.
  assert len(posted) == 1
  assert posted[0][0] == chat.id
  assert "due now" in posted[0][1]
  assert posted[0][2] is True
  # It is now done; the future one is still pending.
  pending = self_reminders.list_pending(chat.id)
  assert [p["note"] for p in pending] == ["future"]


def test_dispatch_retires_reminder_for_deleted_chat(client, auth, monkeypatch):
  """A reminder whose chat was deleted is cancelled, not retried forever."""
  _enable_dispatch()
  self_reminders.enqueue("ghost-chat", "orphan", due_at=1, now=0)

  async def _fake_send(**kwargs):  # should never be called
    raise AssertionError("send_message called for a deleted chat")

  monkeypatch.setattr(
    "app.routes.self_reminders._send_checkin", _fake_send,
  )
  r = client.post("/api/self-reminders/dispatch", headers=auth)
  assert r.status_code == 200
  assert r.json()["fired"] == 0
  assert self_reminders.list_pending("ghost-chat") == []
