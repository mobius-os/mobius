"""Tests for the per-app SSE event stream (feature 214).

The installed standalone PWA's "Updated - tap to refresh" pill subscribes to
`GET /api/apps/{id}/events`. Two boundaries are load-bearing and tested here:

- AUTH boundary: an app-scoped token may open ONLY its own app's stream. A
  token for a different app is 403; a token for an uninstalled app is 401.
  The owner token may open any app's stream.
- FILTER boundary: only THIS app's own `app_updated` events are forwarded -
  never another app's update, never an owner-only system type (theme /
  shell_* / chat_run_*). This is what keeps the app token least-privileged:
  read-only visibility of its own update events and nothing else.
"""

import pytest

from app import models
from app.broadcast import get_system_broadcast
from app.deps import Principal
from app.routes.apps import _app_stream_should_forward, stream_app_events


def _make_app(client, owner_token, name="evt-app"):
  r = client.post(
    "/api/apps/",
    json={
      "name": name,
      "description": "test",
      "jsx_source": "export default function App() { return <div>hi</div> }",
    },
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  return r.json()["id"]


def _app_token(client, owner_token, app_id):
  r = client.post(
    "/api/auth/app-token",
    json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  return r.json()["token"]


class _FakeRequest:
  """Stands in for the Starlette Request the stream generator reads.

  The generator only calls `is_disconnected()`; returning True after a bounded
  number of calls guarantees the while-loop terminates even if the event under
  test is never forwarded, so a filter regression fails loudly instead of
  hanging on the 30s keepalive."""

  def __init__(self, disconnect_after=25):
    self.calls = 0
    self._after = disconnect_after

  async def is_disconnected(self):
    self.calls += 1
    return self.calls > self._after


# --- Filter boundary (pure) -------------------------------------------


def test_forward_own_app_updated():
  assert _app_stream_should_forward(
    {"type": "app_updated", "appId": "5"}, 5) is True


def test_forward_matches_across_str_and_int_appid():
  # appId arrives as a string on the wire; the path id is an int.
  assert _app_stream_should_forward(
    {"type": "app_updated", "appId": "5"}, 5) is True
  assert _app_stream_should_forward(
    {"type": "app_updated", "appId": 5}, 5) is True


def test_drop_other_apps_update():
  assert _app_stream_should_forward(
    {"type": "app_updated", "appId": "6"}, 5) is False


@pytest.mark.parametrize("etype", [
  "theme_updated",
  "shell_rebuilding",
  "shell_rebuilt",
  "shell_apply_now",
  "shell_rebuild_failed",
  "chat_run_started",
  "chat_run_finished",
])
def test_drop_owner_only_event_types(etype):
  # Even carrying this app's id, a non-app_updated (owner-scoped) type is
  # never forwarded onto an app's stream.
  assert _app_stream_should_forward({"type": etype, "appId": "5"}, 5) is False


# --- Auth boundary (endpoint) -----------------------------------------


def test_events_requires_auth(client, owner_token):
  app_id = _make_app(client, owner_token)
  r = client.get(f"/api/apps/{app_id}/events")
  assert r.status_code == 401


def test_app_token_cannot_watch_other_app(client, owner_token):
  """The mandatory auth boundary: app A's token is 403 on app B's stream."""
  a = _make_app(client, owner_token, "app-a")
  b = _make_app(client, owner_token, "app-b")
  a_token = _app_token(client, owner_token, a)
  r = client.get(
    f"/api/apps/{b}/events",
    headers={"Authorization": f"Bearer {a_token}"},
  )
  assert r.status_code == 403


def test_deleted_app_token_cannot_watch(client, owner_token):
  """An app token stops opening its stream the instant the app is uninstalled
  (get_principal rejects a token whose app row is gone)."""
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  assert client.delete(
    f"/api/apps/{app_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  ).status_code == 204
  r = client.get(
    f"/api/apps/{app_id}/events",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 401


def test_events_404_for_unknown_app(client, owner_token):
  """An owner token opening a nonexistent app's stream 404s (the app-scope
  check already 401s an app token for a gone app; the 404 covers the owner)."""
  r = client.get(
    "/api/apps/999999/events",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 404


# --- Delivery + filter, end-to-end on a live stream -------------------


@pytest.mark.asyncio
async def test_app_token_receives_own_update_but_not_others(
  client, owner_token, db,
):
  """The mandatory positive + negative: an app token's own stream forwards its
  OWN app_updated, and drops both another app's update and an owner-only
  system event, on a real streaming response."""
  app_id = _make_app(client, owner_token, "self")
  other_id = _make_app(client, owner_token, "other")
  owner = db.query(models.Owner).one()

  resp = await stream_app_events(
    app_id=app_id,
    request=_FakeRequest(),
    principal=Principal(owner=owner, app_id=app_id),
    db=db,
  )

  sb = get_system_broadcast()
  # Owner-only + other-app events must be dropped; the own update forwarded.
  sb.publish({"type": "theme_updated"})
  sb.publish({"type": "app_updated", "appId": str(other_id)})
  sb.publish({"type": "app_updated", "appId": str(app_id)})

  frames = []
  try:
    async for chunk in resp.body_iterator:
      text = chunk if isinstance(chunk, str) else chunk.decode()
      frames.append(text)
      if "app_updated" in text:
        break
  finally:
    await resp.body_iterator.aclose()

  joined = "".join(frames)
  assert "app_stream_open" in joined, "missing the stream-open hello frame"
  assert '"app_updated"' in joined, "own app_updated was not forwarded"
  assert f'"appId": "{app_id}"' in joined
  # Negative half: the owner-only theme event and the other app's update
  # never reached this stream.
  assert "theme_updated" not in joined
  assert f'"appId": "{other_id}"' not in joined


@pytest.mark.asyncio
async def test_owner_token_can_open_any_app_stream(client, owner_token, db):
  """The owner (app_id is None) may open any app's stream — the gate only
  restricts APP tokens to their own app."""
  from starlette.responses import StreamingResponse

  app_id = _make_app(client, owner_token)
  owner = db.query(models.Owner).one()
  resp = await stream_app_events(
    app_id=app_id,
    request=_FakeRequest(disconnect_after=0),
    principal=Principal(owner=owner, app_id=None),
    db=db,
  )
  assert isinstance(resp, StreamingResponse)
  await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_cross_app_principal_is_rejected_before_streaming(
  client, owner_token, db,
):
  """The 403 gate raises before any StreamingResponse is built — a mismatched
  app principal can never reach the subscribe/stream path."""
  from fastapi import HTTPException

  a = _make_app(client, owner_token, "a")
  b = _make_app(client, owner_token, "b")
  owner = db.query(models.Owner).one()
  with pytest.raises(HTTPException) as exc:
    await stream_app_events(
      app_id=b,
      request=_FakeRequest(),
      principal=Principal(owner=owner, app_id=a),
      db=db,
    )
  assert exc.value.status_code == 403
