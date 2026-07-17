"""POST /api/notify {open_item} — the explicit agent-initiated workspace open.

Covers the strict NotifyBody whitelist (accept + 422 matrix), the system-bus-only
fan-out classification, and the spoof/absent-item guard contract (the backend
publishes what it validated; existence is the Shell's confirm-guard, not the API's).
See split-pane design §6.3.
"""

import asyncio

import pytest

from app import broadcast as bc_mod
from app.broadcast import get_system_broadcast


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
async def test_open_item_accepts_a_well_formed_but_nonexistent_item(client, auth):
  """The backend does NOT verify the item exists — it publishes what it
  validated. Guarding against a spoofed/absent id is the Shell's confirm-before-
  place responsibility (it refetches the list and no-ops when the id is absent),
  so a well-formed request for a non-existent app is a valid 204 here."""
  sb = get_system_broadcast()
  q = sb.subscribe()
  try:
    r = client.post(
      "/api/notify",
      headers=auth,
      json=_open_item_body(itemId="999999"),
    )
    assert r.status_code == 204, r.text
    ev = await asyncio.wait_for(q.get(), timeout=1.0)
    assert ev["itemId"] == "999999"
  finally:
    sb.unsubscribe(q)
