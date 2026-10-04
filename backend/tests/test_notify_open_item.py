"""POST /api/notify {open_item} — the explicit agent-initiated workspace open.

Covers the strict NotifyBody whitelist (accept + 422 matrix), the system-bus-only
fan-out classification, and the absent-item refusal (a missing or deleted item
is a 404, so no caller is told something opened; the Shell still confirms
before placing). See split-pane design §6.3.
"""
from app.chat_writer import create_chat

import asyncio

import pytest

from app import broadcast as bc_mod
from app import models
from app.broadcast import get_system_broadcast
from app.timeutil import now_naive_utc


@pytest.fixture(autouse=True)
def live_items(db):
  """The app and chat ids these tests open, so the existence check passes."""
  for app_id in (7, 42):
    db.add(models.App(
      id=app_id, slug=f"open-item-{app_id}", name=f"App {app_id}",
      source_dir=f"/tmp/mobius-tests/open-item-{app_id}",
      jsx_source="export default function App(){}", compiled_path="/tmp/app.js",
    ))
  db.add(create_chat(id="chat-z", title="Chat Z", messages=[]))
  db.commit()


def _open_item_body(**overrides):
  body = {
    "type": "open_item",
    "itemKind": "app",
    "itemId": "42",
    "sourceKind": "chat",
    "sourceId": "chat-a",
    "placement": "beside-source",
    "activation": "background",
  }
  body.update(overrides)
  return body


@pytest.mark.asyncio
async def test_open_item_accepted_and_reaches_system_broadcast(client, auth):
  """A well-formed open_item is 204 and lands on the SystemBroadcast with its
  typed request fields carried through verbatim."""
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post("/api/notify", headers=auth, json=_open_item_body())
    assert r.status_code == 204, r.text
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev == {
      "type": "open_item",
      "itemKind": "app",
      "itemId": "42",
      "sourceKind": "chat",
      "sourceId": "chat-a",
      "placement": "beside-source",
      "activation": "background",
    }
  finally:
    sb.unsubscribe(q)


@pytest.mark.asyncio
async def test_open_item_without_source_is_accepted(client, auth):
  """A sourceless open_item (with-focus) is valid; the omitted fields are simply
  absent on the emitted event."""
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post(
      "/api/notify",
      headers=auth,
      json={"type": "open_item", "itemKind": "chat", "itemId": "chat-z"},
    )
    assert r.status_code == 204, r.text
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev == {"type": "open_item", "itemKind": "chat", "itemId": "chat-z"}
  finally:
    sb.unsubscribe(q)


@pytest.mark.parametrize(
  "body",
  [
    # itemKind missing / not in {app, chat}.
    {"type": "open_item", "itemId": "42"},
    {"type": "open_item", "itemKind": "widget", "itemId": "42"},
    # itemId missing / empty / whitespace-only.
    {"type": "open_item", "itemKind": "app"},
    {"type": "open_item", "itemKind": "app", "itemId": ""},
    {"type": "open_item", "itemKind": "app", "itemId": "   "},
    # sourceId present-but-empty / whitespace passes the None-pairing check but
    # names nothing — reject it at the wire rather than emit a 204 the shell drops.
    {"type": "open_item", "itemKind": "app", "itemId": "1",
     "sourceKind": "chat", "sourceId": ""},
    {"type": "open_item", "itemKind": "app", "itemId": "1",
     "sourceKind": "chat", "sourceId": "  "},
    # placement / activation not in their closed enums.
    {"type": "open_item", "itemKind": "app", "itemId": "1", "placement": "split-right"},
    {"type": "open_item", "itemKind": "app", "itemId": "1", "activation": "urgent"},
    # source kind + id must travel together, and the kind is enum-checked.
    {"type": "open_item", "itemKind": "app", "itemId": "1", "sourceKind": "chat"},
    {"type": "open_item", "itemKind": "app", "itemId": "1", "sourceId": "c"},
    {"type": "open_item", "itemKind": "app", "itemId": "1",
     "sourceKind": "widget", "sourceId": "c"},
    # an APP itemId / sourceId must be NUMERIC — app tabs dedup on a numeric id,
    # so a non-numeric one would become NaN and silently land in the wrong pane.
    {"type": "open_item", "itemKind": "app", "itemId": "not-a-number"},
    {"type": "open_item", "itemKind": "app", "itemId": "1.5"},
    {"type": "open_item", "itemKind": "app", "itemId": "1",
     "sourceKind": "app", "sourceId": "not-a-number"},
    # foreign fields may not ride an open_item.
    {"type": "open_item", "itemKind": "app", "itemId": "1", "label": "hi"},
    {"type": "open_item", "itemKind": "app", "itemId": "1", "chatId": "c"},
    {"type": "open_item", "itemKind": "app", "itemId": "1", "appId": "1"},
    # an unknown key is rejected outright (extra="forbid").
    {"type": "open_item", "itemKind": "app", "itemId": "1", "ratio": 0.5},
  ],
)
def test_open_item_422_matrix(client, auth, body):
  """Every malformed open_item is a 422 — the whitelist is real, not advisory."""
  r = client.post("/api/notify", headers=auth, json=body)
  assert r.status_code == 422, r.text


@pytest.mark.parametrize(
  "body",
  [
    # open_item fields may not ride a non-open_item event.
    {"type": "app_updated", "appId": "1", "itemKind": "app"},
    {"type": "build_phase", "chatId": "c", "label": "x", "placement": "with-focus"},
    # an unknown key is rejected on any type now, not just open_item.
    {"type": "app_updated", "appId": "1", "bogus": True},
  ],
)
def test_open_item_fields_confined_and_extras_forbidden(client, auth, body):
  """open_item's fields are confined to open_item, and extra="forbid" applies to
  every type — a stray key can no longer be silently ignored."""
  r = client.post("/api/notify", headers=auth, json=body)
  assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_open_item_is_system_bus_only(client, auth):
  """open_item is catch-up-UNSAFE (an action): it rides the system broadcast
  ALONE and never fans out to a per-chat broadcast, so a chat reconnect's replay
  cannot re-open the item a second time."""
  chat = bc_mod.create_broadcast("open-item-chat")
  q_chat = chat.subscribe()[1]
  sb = get_system_broadcast()
  q_sys = sb.subscribe()
  try:
    r = client.post("/api/notify", headers=auth, json=_open_item_body())
    assert r.status_code == 204, r.text
    ev_sys = await asyncio.wait_for(q_sys.get(), timeout=1.0)
    assert ev_sys["type"] == "open_item"
    # No fan-out, no replay entry on the per-chat broadcast.
    assert all(e.get("type") != "open_item" for e in chat.event_log), chat.event_log
    with pytest.raises(asyncio.TimeoutError):
      await asyncio.wait_for(q_chat.get(), timeout=0.2)
  finally:
    sb.unsubscribe(q_sys)
    bc_mod.remove_broadcast("open-item-chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,item_id", [
  ("app", "999999"),
  ("app", "99999999999999999999"),
  ("chat", "chat-missing"),
  ("app", "42"),
  ("chat", "chat-z"),
])
async def test_open_item_refuses_a_missing_or_deleted_item(client, auth, db, kind, item_id):
  """A well-formed open_item for an item that does not exist, or was deleted,
  is a 404 and publishes nothing, so the caller never reports it opened."""
  for model, key in ((models.App, 42), (models.Chat, "chat-z")):
    db.get(model, key).deleted_at = now_naive_utc()
  db.commit()
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post(
      "/api/notify", headers=auth,
      json={"type": "open_item", "itemKind": kind, "itemId": item_id},
    )
    assert r.status_code == 404, r.text
    assert "nothing was opened" in r.json()["detail"]
    with pytest.raises(asyncio.TimeoutError):
      await asyncio.wait_for(q.get(), timeout=0.2)
  finally:
    sb.unsubscribe(q)


@pytest.mark.asyncio
async def test_open_item_numeric_app_source_accepted(client, auth):
  """A numeric app sourceId is valid (it names a real app tab), while a chat
  sourceId stays free-form — only app ids are constrained to be numeric."""
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post(
      "/api/notify", headers=auth,
      json=_open_item_body(itemId="7", sourceKind="app", sourceId="42"),
    )
    assert r.status_code == 204, r.text
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev["sourceKind"] == "app" and ev["sourceId"] == "42"
  finally:
    sb.unsubscribe(q)
