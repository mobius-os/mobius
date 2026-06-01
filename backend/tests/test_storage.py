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


def test_list_returns_entries_with_metadata(client, auth, owner_token):
  """Listing a directory returns deterministic entries + metadata."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/reports/2026-06-01.html",
    data="<p>hi</p>",
    headers={**auth, "Content-Type": "text/html"},
  )
  client.put(
    f"/api/storage/apps/{app_id}/reports/2026-06-02.json",
    json={"k": 1},
    headers=auth,
  )

  r = client.get(f"/api/storage/apps-list/{app_id}/reports", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert body["next_cursor"] is None
  entries = body["entries"]
  # Sorted lexically by name → deterministic order.
  assert [e["name"] for e in entries] == [
    "2026-06-01.html",
    "2026-06-02.json",
  ]
  html = entries[0]
  assert html["path"] == "reports/2026-06-01.html"
  assert html["type"] == "file"
  assert html["size"] == len("<p>hi</p>")
  assert html["mime_type"] == "text/html"
  # ISO-8601 UTC with a trailing Z.
  assert html["modified_at"].endswith("Z")


def test_list_includes_directories(client, auth, owner_token):
  """Immediate-child directories show up with type 'directory'."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/nested/inner.json",
    json={"k": 1},
    headers=auth,
  )

  r = client.get(f"/api/storage/apps-list/{app_id}/", headers=auth)
  assert r.status_code == 200
  entries = r.json()["entries"]
  by_name = {e["name"]: e for e in entries}
  assert by_name["nested"]["type"] == "directory"
  assert by_name["nested"]["path"] == "nested"
  # Directories carry no mime_type.
  assert "mime_type" not in by_name["nested"]


def test_list_root_and_nested(client, auth, owner_token):
  """Root listing and nested listing both work."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/top.json", json={"k": 1}, headers=auth,
  )
  client.put(
    f"/api/storage/apps/{app_id}/sub/deep.json", json={"k": 2}, headers=auth,
  )

  root = client.get(f"/api/storage/apps-list/{app_id}/", headers=auth)
  assert root.status_code == 200
  root_names = {e["name"] for e in root.json()["entries"]}
  assert {"top.json", "sub"} <= root_names

  nested = client.get(f"/api/storage/apps-list/{app_id}/sub", headers=auth)
  assert nested.status_code == 200
  assert [e["name"] for e in nested.json()["entries"]] == ["deep.json"]


def test_list_skips_symlinks_and_unsafe_names(client, auth, owner_token):
  """Listings omit symlinks and names the read/PUT whitelist rejects, so
  every listed entry round-trips back through get()/put()."""
  import os
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/safe/ok.json", json={"k": 1}, headers=auth,
  )
  base = os.path.join(os.environ["DATA_DIR"], "apps", str(app_id), "safe")
  # Created on disk directly, bypassing the storage API: a symlink
  # (following it could leak a target outside the tree) and a file whose
  # name _SAFE_RE rejects. Both must be absent from the listing.
  os.symlink("/etc/hostname", os.path.join(base, "link.json"))
  with open(os.path.join(base, "bad name.json"), "w") as f:
    f.write("{}")

  r = client.get(f"/api/storage/apps-list/{app_id}/safe", headers=auth)
  assert r.status_code == 200
  names = {e["name"] for e in r.json()["entries"]}
  assert names == {"ok.json"}


def test_list_missing_dir_returns_empty(client, auth, owner_token):
  """Listing a not-yet-created directory is empty, NOT a 404."""
  app_id = _make_app(client, owner_token)
  r = client.get(f"/api/storage/apps-list/{app_id}/ghost", headers=auth)
  assert r.status_code == 200
  assert r.json() == {"entries": [], "next_cursor": None}


def test_list_rejects_traversal(client, auth, owner_token):
  """A traversal prefix is rejected by the same containment check.

  The `..` segments are percent-encoded so the HTTP client doesn't
  normalize them away before the request leaves — the server decodes
  them back to literal `..` parts, which `_resolve` rejects with 400.
  """
  app_id = _make_app(client, owner_token)
  r = client.get(
    f"/api/storage/apps-list/{app_id}/%2e%2e/%2e%2e/etc", headers=auth,
  )
  assert r.status_code == 400


def test_list_pagination(client, auth, owner_token):
  """limit + opaque cursor walk the directory in deterministic pages."""
  app_id = _make_app(client, owner_token)
  for i in range(5):
    client.put(
      f"/api/storage/apps/{app_id}/items/{i:02d}.json",
      json={"i": i},
      headers=auth,
    )

  page1 = client.get(
    f"/api/storage/apps-list/{app_id}/items?limit=2", headers=auth,
  ).json()
  assert [e["name"] for e in page1["entries"]] == ["00.json", "01.json"]
  assert page1["next_cursor"] is not None

  page2 = client.get(
    f"/api/storage/apps-list/{app_id}/items?limit=2"
    f"&cursor={page1['next_cursor']}",
    headers=auth,
  ).json()
  assert [e["name"] for e in page2["entries"]] == ["02.json", "03.json"]
  assert page2["next_cursor"] is not None

  page3 = client.get(
    f"/api/storage/apps-list/{app_id}/items?limit=2"
    f"&cursor={page2['next_cursor']}",
    headers=auth,
  ).json()
  assert [e["name"] for e in page3["entries"]] == ["04.json"]
  # Last page is exhausted.
  assert page3["next_cursor"] is None
