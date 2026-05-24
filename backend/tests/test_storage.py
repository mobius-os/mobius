"""Storage API: tests for both envelope and inner-object PUT forms."""

import json


def _make_app(client, owner_token):
  r = client.post("/api/apps/", json={
    "name": "store-test",
    "description": "test",
    "jsx_source": "export default function App() { return <div/> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  return r.json()["id"]


def test_put_json_inner_object(client, auth, owner_token):
  """PUT body is the inner object; server stringifies + writes."""
  app_id = _make_app(client, owner_token)
  data = {"title": "hi", "items": [1, 2, 3]}

  r = client.put(
    f"/api/storage/apps/{app_id}/notes.json",
    json=data,
    headers=auth,
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/notes.json", headers=auth)
  assert r.status_code == 200
  assert r.json() == data


def test_put_json_body_stored_as_is(client, auth, owner_token):
  """`.json` paths store the JSON body literally, including dicts
  that happen to be shaped like the legacy envelope. Without this,
  any mini-app whose document shape is `{"content": "..."}` would
  be silently unwrapped on write and the next read would surface a
  raw string where the app expected a dict.
  """
  app_id = _make_app(client, owner_token)
  # A mini-app legitimately storing a single-field doc shaped like
  # the legacy envelope — must round-trip unchanged.
  doc = {"content": "this is my document body, not an envelope"}

  r = client.put(
    f"/api/storage/apps/{app_id}/notes.json",
    json=doc,
    headers=auth,
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/notes.json", headers=auth)
  assert r.json() == doc


def test_put_text_requires_envelope(client, auth, owner_token):
  """Non-JSON paths must use the {content: "..."} envelope form."""
  app_id = _make_app(client, owner_token)

  r = client.put(
    f"/api/storage/apps/{app_id}/notes.txt",
    json={"content": "plain text body"},
    headers=auth,
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/notes.txt", headers=auth)
  assert r.text == "plain text body"


def test_put_text_accepts_raw_text_body(client, auth, owner_token):
  """Non-JSON text paths accept raw UTF-8 text without an envelope."""
  app_id = _make_app(client, owner_token)

  r = client.put(
    f"/api/storage/apps/{app_id}/notes.txt",
    data="plain text body",
    headers={**auth, "Content-Type": "text/plain; charset=utf-8"},
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/notes.txt", headers=auth)
  assert r.text == "plain text body"


def test_put_binary_accepts_raw_bytes(client, auth, owner_token):
  """Binary writes store raw bytes directly."""
  app_id = _make_app(client, owner_token)
  data = b"\x00\x01\x02raw\xff"

  r = client.put(
    f"/api/storage/apps/{app_id}/blob.bin",
    data=data,
    headers={**auth, "Content-Type": "application/octet-stream"},
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/blob.bin", headers=auth)
  assert r.content == data


def test_put_text_rejects_inner_object(client, auth, owner_token):
  """Non-JSON path with inner-object body is a 400 — would be ambiguous
  on read."""
  app_id = _make_app(client, owner_token)

  r = client.put(
    f"/api/storage/apps/{app_id}/notes.txt",
    json={"title": "hi"},
    headers=auth,
  )
  assert r.status_code == 400


def test_put_json_array_body(client, auth, owner_token):
  """Top-level arrays count as inner objects too."""
  app_id = _make_app(client, owner_token)

  r = client.put(
    f"/api/storage/apps/{app_id}/list.json",
    json=[1, 2, 3],
    headers=auth,
  )
  assert r.status_code == 204

  r = client.get(f"/api/storage/apps/{app_id}/list.json", headers=auth)
  assert r.json() == [1, 2, 3]


def test_put_shared_inner_object(client, auth):
  """Shared storage PUT also accepts inner-object form."""
  r = client.put(
    "/api/storage/shared/config.json",
    json={"theme": "dark"},
    headers=auth,
  )
  assert r.status_code == 204

  r = client.get("/api/storage/shared/config.json", headers=auth)
  assert r.json() == {"theme": "dark"}


def test_put_text_accepts_non_json_content_type(client, auth, owner_token):
  app_id = _make_app(client, owner_token)

  r = client.put(
    f"/api/storage/apps/{app_id}/notes.txt",
    data="raw body",
    headers={**auth, "Content-Type": "text/plain"},
  )
  assert r.status_code == 204
