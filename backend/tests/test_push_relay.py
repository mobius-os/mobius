"""The mobius.you push relay wire contract, against a fake relay.

``app.push_relay`` reaches the relay only through the identity broker's
socket, so these tests replace that client with an in-process fake relay and
check exactly what crosses the wire and how each reply is classified.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import push_relay


def _use_relay(monkeypatch, handler):
  def client(*, timeout):
    del timeout
    return httpx.Client(base_url="http://broker", transport=httpx.MockTransport(handler))
  monkeypatch.setattr(push_relay, "broker_client", client)


def _send(tokens):
  return push_relay.send(
    tokens, title="Done", body="Your app is ready.",
    data={"url": "https://mobius.example/shell/?chat=c1", "notificationId": "n1"},
    collapse_key="agent:c1:done",
  )


def test_send_carries_the_notification_string_data_and_an_idempotency_key(monkeypatch):
  seen = []

  def relay(request):
    seen.append(request)
    return httpx.Response(200, json={"results": [
      {"index": 0, "status": "ok"}, {"index": 1, "status": "invalid_token"},
    ]})

  _use_relay(monkeypatch, relay)

  assert _send(["a", "b"]) == ["ok", "invalid_token"]
  request = seen[0]
  assert request.method == "POST" and request.url.path == "/v1/push/send"
  assert json.loads(request.content) == {
    "targets": [{"provider": "fcm", "token": "a"}, {"provider": "fcm", "token": "b"}],
    "notification": {"title": "Done", "body": "Your app is ready."},
    "data": {"url": "https://mobius.example/shell/?chat=c1", "notificationId": "n1"},
    "options": {"collapseKey": "agent:c1:done", "ttlSeconds": 86400, "priority": "high"},
  }
  assert request.headers["idempotency-key"].startswith("push:")


def test_a_missing_or_malformed_result_is_an_error_never_a_silent_success(monkeypatch):
  _use_relay(monkeypatch, lambda request: httpx.Response(200, json={"results": [
    {"index": 1, "status": "ok"},
    {"index": 0, "status": "delivered"},
    {"index": 9, "status": "ok"},
    {"index": True, "status": "ok"},
    {"index": 2, "status": ["ok"]},
    {"index": 2, "status": {"ok": True}},
    "not an object",
  ]}))
  assert _send(["a", "b", "c"]) == ["error", "ok", "error"]

  _use_relay(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
  assert _send(["a"]) == ["error"]


@pytest.mark.parametrize(("status", "not_linked"), [(401, True), (403, False), (503, False)])
def test_a_refused_request_raises_and_says_whether_linking_is_missing(
  monkeypatch, status, not_linked,
):
  _use_relay(monkeypatch, lambda request: httpx.Response(status, json={"error": "no"}))

  with pytest.raises(push_relay.RelayError) as raised:
    push_relay.bind_device("t")
  assert raised.value.not_linked is not_linked


def test_an_unreachable_broker_raises(monkeypatch):
  def relay(request):
    raise httpx.ConnectError("no socket", request=request)

  _use_relay(monkeypatch, relay)
  with pytest.raises(push_relay.RelayError):
    _send(["a"])


def test_bind_and_unbind_payloads_use_a_fresh_key_each_call(monkeypatch):
  seen = []

  def relay(request):
    seen.append((request.url.path, json.loads(request.content), request.headers["idempotency-key"]))
    return httpx.Response(200, json={})

  _use_relay(monkeypatch, relay)
  push_relay.bind_device("t")
  push_relay.bind_device("t")
  push_relay.unbind_device("t")

  assert seen[0][:2] == ("/v1/push/devices/bind", {"provider": "fcm", "token": "t"})
  assert seen[1][1] == {"provider": "fcm", "token": "t"}
  assert seen[2][:2] == ("/v1/push/devices/unbind", {"token": "t"})
  # A phone that moved away and back must be re-bound, not answered from cache.
  assert len({key for _, _, key in seen}) == 3


def test_notifications_are_shortened_to_fit_the_relay_limits(monkeypatch):
  seen = []

  def relay(request):
    seen.append(json.loads(request.content))
    return httpx.Response(200, json={"results": [{"index": 0, "status": "ok"}]})

  _use_relay(monkeypatch, relay)
  data = {"url": "https://mobius.example/shell/", "notificationId": "n1"}
  push_relay.send(["a"], title="T" * 500, body='ü"\n' * 4000, data=data, collapse_key="k")

  sent = seen[0]
  title, body = sent["notification"]["title"], sent["notification"]["body"]
  assert len(title) == push_relay.MAX_TITLE_CHARS and title.endswith("…")
  assert len(body) <= push_relay.MAX_BODY_CHARS and body.endswith("…")
  assert sent["data"] == data
  assert len(json.dumps(
    {"title": title, "body": body, "data": data},
    separators=(",", ":"), ensure_ascii=False,
  ).encode()) <= push_relay.MAX_PAYLOAD_BYTES


def test_short_notifications_are_sent_unchanged():
  data = {"url": "https://mobius.example/shell/", "notificationId": "n1"}
  assert push_relay.fit_notification("Done", "Ready.", data) == ("Done", "Ready.")


def test_the_title_is_shortened_too_when_the_link_leaves_little_room():
  data = {"url": "https://mobius.example/" + "ü" * 1500, "notificationId": "n1"}

  title, body = push_relay.fit_notification("Tïtle " * 60, "ü" * 2000, data)

  assert body == "" and title and title.endswith("…")
  assert len(json.dumps(
    {"title": title, "body": body, "data": data},
    separators=(",", ":"), ensure_ascii=False,
  ).encode()) <= push_relay.MAX_PAYLOAD_BYTES
