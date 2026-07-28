"""App-scoped client signals are safely attributed to the activity stream."""
import json
from pathlib import Path

from app.config import get_settings
from app import activity, auth
from app.routes import client_signal
from test_app_fixtures import create_local_app


def _activity_lines() -> list[dict]:
  path = Path(get_settings().data_dir) / "logs" / "activity.jsonl"
  if not path.exists():
    return []
  return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _make_app(client, owner_token) -> int:
  return create_local_app(
    client,
    {"Authorization": f"Bearer {owner_token}"},
    name="signalling-app",
  )["id"]


def _app_token(client, owner_token, app_id) -> str:
  response = client.post(
    "/api/auth/app-token",
    json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  return response.json()["token"]


def _batch(token: str, signals: list[dict]) -> dict:
  nonce = auth.decode_access_token(token).get("app_nonce")
  return {"signals": [
    {**signal, "app_instance_id": nonce} for signal in signals
  ]}


def test_app_signal_batch_is_attributed_and_keeps_server_ingestion_time(
  client, owner_token,
):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  occurred_at = "2026-07-13T12:34:56Z"

  response = client.post(
    "/api/client-signal",
    json=_batch(token, [
      {
        "id": "signal-one",
        "occurred_at": occurred_at,
        "name": "app_ready",
        "payload": {"item_count": 3},
      },
      {
        "id": "signal-two",
        "occurred_at": occurred_at,
        "name": "item_created",
        "payload": {"type": "note"},
      },
    ]),
    headers={"Authorization": f"Bearer {token}"},
  )
  assert response.status_code == 204, response.text

  events = [event for event in _activity_lines() if event.get("ev") == "app_signal"]
  assert [event["id"] for event in events] == ["signal-one", "signal-two"]
  assert all(event["app_id"] == app_id for event in events)
  assert events[0]["occurred_at"] == occurred_at
  assert events[0]["ts"] != events[0]["occurred_at"]
  assert events[0]["payload"] == {"item_count": 3}


def test_owner_token_cannot_emit_an_unattributed_app_signal(client, owner_token):
  response = client.post(
    "/api/client-signal",
    json={"signals": [{
      "id": "owner-signal",
      "occurred_at": "2026-07-13T12:34:56Z",
      "name": "app_ready",
    }]},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 403
  assert not [event for event in _activity_lines() if event.get("ev") == "app_signal"]


def test_signal_installation_identity_must_match_app_token(client, owner_token):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  app_nonce = auth.decode_access_token(token)["app_nonce"]
  base = {
    "id": "instance-bound",
    "occurred_at": "2026-07-13T12:34:56Z",
    "name": "app_ready",
  }
  headers = {"Authorization": f"Bearer {token}"}

  mismatch = client.post(
    "/api/client-signal",
    json={"signals": [{**base, "app_instance_id": "another-install"}]},
    headers=headers,
  )
  assert mismatch.status_code == 409

  missing = client.post(
    "/api/client-signal",
    json={"signals": [base]},
    headers=headers,
  )
  assert missing.status_code == 409

  accepted = client.post(
    "/api/client-signal",
    json={"signals": [{**base, "app_instance_id": app_nonce}]},
    headers=headers,
  )
  assert accepted.status_code == 204
  events = [event for event in _activity_lines() if event.get("id") == "instance-bound"]
  assert len(events) == 1
  assert events[0]["app_instance_id"] == app_nonce


def test_signal_batch_and_payload_are_bounded(client, owner_token):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  headers = {"Authorization": f"Bearer {token}"}
  base = {
    "id": "bounded",
    "occurred_at": "2026-07-13T12:34:56Z",
    "name": "item_created",
  }

  too_many = client.post(
    "/api/client-signal",
    json=_batch(token, [{**base, "id": f"signal-{i}"} for i in range(101)]),
    headers=headers,
  )
  assert too_many.status_code == 422

  oversized_value = client.post(
    "/api/client-signal",
    json=_batch(token, [{**base, "payload": {"message": "x" * 501}}]),
    headers=headers,
  )
  assert oversized_value.status_code == 422
  assert not [event for event in _activity_lines() if event.get("ev") == "app_signal"]

  duplicate_ids = client.post(
    "/api/client-signal",
    json=_batch(token, [base, base]),
    headers=headers,
  )
  assert duplicate_ids.status_code == 422


def test_signal_ingest_returns_retryable_failure_when_activity_write_fails(
  client, owner_token, monkeypatch,
):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  monkeypatch.setattr(activity, "log_events", lambda events, **kwargs: False)

  response = client.post(
    "/api/client-signal",
    json=_batch(token, [{
      "id": "retry-me",
      "occurred_at": "2026-07-13T12:34:56Z",
      "name": "app_ready",
    }]),
    headers={"Authorization": f"Bearer {token}"},
  )
  assert response.status_code == 503


def test_signal_rate_limit_bounds_one_app_without_trusting_the_client(
  client, owner_token, monkeypatch,
):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  monkeypatch.setattr(activity, "log_events", lambda events, **kwargs: True)
  headers = {"Authorization": f"Bearer {token}"}
  batch = [{
    "id": f"rate-{i}",
    "occurred_at": "2026-07-13T12:34:56Z",
    "name": "item_created",
  } for i in range(100)]

  for page in range(2):
    response = client.post(
      "/api/client-signal",
      json=_batch(token, [{**event, "id": f"{page}-{event['id']}"} for event in batch]),
      headers=headers,
    )
    assert response.status_code == 204
  response = client.post(
    "/api/client-signal",
    json=_batch(token, [{**batch[0], "id": "over-limit"}]),
    headers=headers,
  )
  assert response.status_code == 429
  assert int(response.headers["Retry-After"]) > 23 * 3600


def test_failed_append_rolls_back_rate_budget(client, owner_token, monkeypatch):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  headers = {"Authorization": f"Bearer {token}"}
  calls = 0

  def fail_once(events, **kwargs):
    nonlocal calls
    calls += 1
    return calls > 1

  monkeypatch.setattr(activity, "log_events", fail_once)
  body = _batch(token, [{
    "id": f"rollback-{i}",
    "occurred_at": "2026-07-13T12:34:56Z",
    "name": "item_created",
  } for i in range(100)])
  assert client.post("/api/client-signal", json=body, headers=headers).status_code == 503
  assert client.post("/api/client-signal", json=body, headers=headers).status_code == 204


def test_replayed_ids_are_acknowledged_without_duplicate_append(
  client, owner_token,
):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  headers = {"Authorization": f"Bearer {token}"}
  body = _batch(token, [{
    "id": "stable-replay",
    "occurred_at": "2026-07-13T12:34:56Z",
    "name": "app_ready",
  }])
  assert client.post("/api/client-signal", json=body, headers=headers).status_code == 204
  assert client.post("/api/client-signal", json=body, headers=headers).status_code == 204
  events = [event for event in _activity_lines() if event.get("id") == "stable-replay"]
  assert len(events) == 1


def test_signal_serialized_bytes_and_future_timestamp_are_bounded(
  client, owner_token,
):
  app_id = _make_app(client, owner_token)
  token = _app_token(client, owner_token, app_id)
  headers = {"Authorization": f"Bearer {token}"}
  oversized = client.post("/api/client-signal", json=_batch(token, [{
    "id": "too-wide",
    "occurred_at": "2026-07-13T12:34:56Z",
    "name": "error",
    "payload": {f"field-{i}": "🙂" * 500 for i in range(20)},
  }]), headers=headers)
  assert oversized.status_code == 422

  future = client.post("/api/client-signal", json=_batch(token, [{
    "id": "future",
    "occurred_at": "2099-01-01T00:00:00Z",
    "name": "app_ready",
  }]), headers=headers)
  assert future.status_code == 422


def test_signal_rate_owners_expire_without_evicting_active_apps(monkeypatch):
  """Only app instances outside the existing rate window are reclaimed."""
  clock = [100.0]
  monkeypatch.setattr(client_signal.time, "monotonic", lambda: clock[0])

  for i in range(4):
    clock[0] += 1
    reservation = client_signal._reserve_rate(
      (i, f"instance-{i}"), [(f"signal-{i}", 10)],
    )
    assert not isinstance(reservation, float)

  assert len(client_signal._recent_by_app) == 4
  assert len(client_signal._seen_ids_by_app) == 4
  assert (0, "instance-0") in client_signal._recent_by_app
  # A retained ID remains a deduplicated no-op.
  _token, novel = client_signal._reserve_rate(
    (3, "instance-3"), [("signal-3", 10)],
  )
  assert novel == set()

  clock[0] += client_signal._RATE_WINDOW_SECONDS + 1
  client_signal._reserve_rate((9, "fresh"), [("fresh", 10)])
  assert set(client_signal._recent_by_app) == {(9, "fresh")}
  assert set(client_signal._seen_ids_by_app) == {(9, "fresh")}


def test_failed_signal_reservation_releases_empty_owner(monkeypatch):
  """Rollback drops the app-instance owner when it retained no other budget."""
  monkeypatch.setattr(client_signal.time, "monotonic", lambda: 100.0)
  app_key = (1, "rolled-back")
  token, novel = client_signal._reserve_rate(app_key, [("retry", 10)])
  client_signal._rollback_rate(app_key, token, novel)
  assert app_key not in client_signal._recent_by_app
  assert app_key not in client_signal._seen_ids_by_app
