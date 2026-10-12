"""Live rooms: a relay between frames of one app, with bounded resources."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import live_rooms
from app.routes import live_rooms as routes


@pytest.fixture
def client(monkeypatch):
  monkeypatch.setattr(routes, "_authorize", lambda token, app_id: token == f"ok-{app_id}")
  live_rooms.hub.rooms.clear()
  live_rooms.hub.connections = 0
  app = FastAPI()
  app.include_router(routes.router)
  with TestClient(app) as c:
    yield c


def url(app_id=7, room="party-AB12", token=None, **q):
  query = "&".join([f"token={token or f'ok-{app_id}'}"] + [f"{k}={v}" for k, v in q.items()])
  return f"/api/apps/{app_id}/live/{room}?{query}"


def test_members_see_each_other_and_exchange_messages(client):
  with client.websocket_connect(url(name="Ana")) as a:
    hello = a.receive_json()
    assert hello["t"] == "welcome" and hello["you"]["name"] == "Ana"
    with client.websocket_connect(url(name="Bo")) as b:
      b_hello = b.receive_json()
      assert [m["name"] for m in b_hello["members"]] == ["Ana", "Bo"]
      assert a.receive_json()["t"] == "presence"
      a.send_json({"t": "send", "data": {"x": 1}})
      got = b.receive_json()
      assert got == {"t": "msg", "from": hello["you"]["id"], "data": {"x": 1}}
      b.send_json({"t": "send", "data": "private", "to": [hello["you"]["id"]]})
      assert a.receive_json()["data"] == "private"
    assert [m["name"] for m in a.receive_json()["members"]] == ["Ana"]


def test_rooms_are_isolated_per_app_and_name(client):
  with client.websocket_connect(url(app_id=1)) as a, client.websocket_connect(url(app_id=2)) as b:
    assert a.receive_json()["members"] and b.receive_json()["members"]
    a.send_json({"t": "send", "data": 1})
    a.send_json({"t": "sync"})
    assert a.receive_json()["t"] == "presence"   # only its own roster comes back


def test_bad_token_is_refused(client):
  with client.websocket_connect(url(app_id=3, token="nope")) as ws:
    with pytest.raises(WebSocketDisconnect) as exc:
      ws.receive_json()
    assert exc.value.code == 4401


def test_full_room_is_refused(client):
  with client.websocket_connect(url(max=2)) as a, client.websocket_connect(url()) as b:
    a.receive_json(); b.receive_json()
    with client.websocket_connect(url()) as c:
      with pytest.raises(WebSocketDisconnect) as exc:
        c.receive_json()
      assert exc.value.code == 4403


def test_oversized_message_closes_connection(client):
  with client.websocket_connect(url()) as a:
    a.receive_json()
    a.send_text("x" * (live_rooms.MAX_MESSAGE_BYTES + 1))
    with pytest.raises(WebSocketDisconnect) as exc:
      a.receive_json()
    assert exc.value.code == 4413


def test_room_is_forgotten_when_empty(client):
  with client.websocket_connect(url()) as a:
    a.receive_json()
    assert live_rooms.hub.rooms
  assert not live_rooms.hub.rooms and live_rooms.hub.connections == 0
