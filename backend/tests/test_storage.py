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


def test_write_is_atomic_no_temp_leftover(client, auth, owner_token):
  """A successful write leaves the target file and NO stray temp file.

  _atomic_write writes to a temp file then os.replace()s it; a torn read
  is impossible and the temp must not survive (Codex review #3)."""
  import os
  app_id = _make_app(client, owner_token)
  r = client.put(
    f"/api/storage/apps/{app_id}/notes.json", json={"k": 1}, headers=auth,
  )
  assert r.status_code == 204
  app_dir = os.path.join(os.environ["DATA_DIR"], "apps", str(app_id))
  names = os.listdir(app_dir)
  assert "notes.json" in names
  assert not [n for n in names if n.endswith(".tmp")]
  assert client.get(
    f"/api/storage/apps/{app_id}/notes.json", headers=auth
  ).json() == {"k": 1}


def test_read_directory_path_404(client, auth, owner_token):
  """GET on a directory path 404s cleanly instead of 500 (Codex review #11)."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/sub/a.json", json={"k": 1}, headers=auth,
  )
  r = client.get(f"/api/storage/apps/{app_id}/sub", headers=auth)
  assert r.status_code == 404


def test_write_to_directory_path_400(client, auth, owner_token):
  """PUT onto an existing directory is a clean 400, not an opaque 500."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/sub/a.json", json={"k": 1}, headers=auth,
  )
  r = client.put(
    f"/api/storage/apps/{app_id}/sub", json={"k": 2}, headers=auth,
  )
  assert r.status_code == 400


def test_symlink_component_rejected(client, auth, owner_token):
  """read/PUT/DELETE through an in-tree symlink are rejected (Codex review #12).

  Listings already omit symlinks; the resolve-based routes now match that
  no-symlink contract, so a DELETE can't remove a link's target."""
  import os
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/real/x.json", json={"k": 1}, headers=auth,
  )
  app_dir = os.path.join(os.environ["DATA_DIR"], "apps", str(app_id))
  os.symlink(os.path.join(app_dir, "real"), os.path.join(app_dir, "link"))
  assert client.get(
    f"/api/storage/apps/{app_id}/link/x.json", headers=auth
  ).status_code == 400
  assert client.put(
    f"/api/storage/apps/{app_id}/link/x.json", json={"k": 2}, headers=auth
  ).status_code == 400
  assert client.delete(
    f"/api/storage/apps/{app_id}/link/x.json", headers=auth
  ).status_code == 400


def test_write_to_nonexistent_app_404(client, auth):
  """Owner cannot create an orphan storage tree for a missing app id.

  _check_cross_app loads the target app first and 404s if absent (Codex
  review #1), so /data/apps/<missing-id> is never created."""
  import os
  r = client.put(
    "/api/storage/apps/999999/x.json", json={"k": 1}, headers=auth,
  )
  assert r.status_code == 404
  assert not os.path.isdir(
    os.path.join(os.environ["DATA_DIR"], "apps", "999999")
  )


def test_shared_list_missing_dir_returns_empty(client, auth):
  """shared-list of a not-yet-created dir is empty, matching apps-list."""
  r = client.get("/api/storage/shared-list/ghost", headers=auth)
  assert r.status_code == 200
  assert r.json() == {"entries": [], "next_cursor": None}


def test_shared_list_paginates_and_skips_symlinks(client, auth):
  """shared-list keyset-paginates and omits symlinks (Codex review #10)."""
  import os
  for i in range(3):
    client.put(
      f"/api/storage/shared/slist/{i:02d}.txt",
      json={"content": str(i)},
      headers=auth,
    )
  shared_dir = os.path.join(os.environ["DATA_DIR"], "shared", "slist")
  os.symlink("/etc/hostname", os.path.join(shared_dir, "evil.txt"))

  page1 = client.get(
    "/api/storage/shared-list/slist?limit=2", headers=auth
  ).json()
  assert [e["name"] for e in page1["entries"]] == ["00.txt", "01.txt"]
  assert page1["next_cursor"] is not None
  page2 = client.get(
    f"/api/storage/shared-list/slist?limit=2&cursor={page1['next_cursor']}",
    headers=auth,
  ).json()
  # The symlink (evil.txt) is dropped, so only the real 02.txt remains.
  assert [e["name"] for e in page2["entries"]] == ["02.txt"]
  assert page2["next_cursor"] is None


def test_numeric_slug_is_prefixed():
  """A purely-numeric name can't become a bare-integer source dir that
  would collide with the numeric-id storage tree (Codex review #4)."""
  from app.routes.apps import _slugify_for_source_dir
  assert _slugify_for_source_dir("123") == "app-123"
  assert not _slugify_for_source_dir("42").isdigit()
  # Non-numeric names are untouched.
  assert _slugify_for_source_dir("news") == "news"
  assert _slugify_for_source_dir("Snake 2") == "snake-2"


def test_create_app_rejects_unsafe_source_dir(client, owner_token):
  """A caller-supplied source_dir must be an immediate, non-numeric child of
  /data/apps — closing both traversal/arbitrary-location and the numeric-id
  storage collision (Codex review #4 + security review)."""
  import os
  data_dir = os.environ["DATA_DIR"]
  auth = {"Authorization": f"Bearer {owner_token}"}
  jsx = "export default function App(){ return <div/> }"
  # Bare-integer dir directly under /data/apps → 400 (storage collision).
  assert client.post("/api/apps/", json={
    "name": "evil", "description": "x", "jsx_source": jsx,
    "source_dir": os.path.join(data_dir, "apps", "123"),
  }, headers=auth).status_code == 400
  # Not an immediate child of /data/apps → 400 (traversal / arbitrary path).
  assert client.post("/api/apps/", json={
    "name": "evil2", "description": "x", "jsx_source": jsx,
    "source_dir": os.path.join(data_dir, "apps", "sub", "deep"),
  }, headers=auth).status_code == 400
  assert client.post("/api/apps/", json={
    "name": "evil3", "description": "x", "jsx_source": jsx,
    "source_dir": os.path.join(data_dir, "apps", "..", "etc"),
  }, headers=auth).status_code == 400
  # A normal slug dir under /data/apps is accepted.
  assert client.post("/api/apps/", json={
    "name": "fine", "description": "x", "jsx_source": jsx,
    "source_dir": os.path.join(data_dir, "apps", "fine"),
  }, headers=auth).status_code == 201


def test_uninstall_skips_numeric_source_dir(client, auth, owner_token, db):
  """Uninstall must NOT rmtree a /data/apps/<number> source_dir — that path is
  another app's STORAGE tree. Defends a legacy row created before source_dir
  validation existed (Codex review #4)."""
  import os
  import app.models as models
  data_dir = os.environ["DATA_DIR"]
  victim = os.path.join(data_dir, "apps", "777")
  os.makedirs(victim, exist_ok=True)
  with open(os.path.join(victim, "keep.json"), "w") as f:
    f.write("{}")
  # Force a legacy-shaped source_dir directly in the DB (the API would now
  # reject it on create/patch).
  app_id = _make_app(client, owner_token)
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.source_dir = victim
  db.commit()
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  # The numeric dir (another app's storage) and its contents survive.
  assert os.path.isdir(victim)
  assert os.path.isfile(os.path.join(victim, "keep.json"))


def test_uninstall_skips_nested_and_shared_source_dir(client, auth, owner_token, db):
  """Uninstall won't rmtree a NESTED descendant of /data/apps (could be inside
  another app's tree) or a dir SHARED by another app row (Codex review #4)."""
  import os
  import app.models as models
  data_dir = os.environ["DATA_DIR"]

  # A nested descendant (not an immediate child) must not be removed.
  nested = os.path.join(data_dir, "apps", "keepme", "inner")
  os.makedirs(nested, exist_ok=True)
  open(os.path.join(nested, "x.json"), "w").close()
  a1 = _make_app(client, owner_token)
  db.query(models.App).filter(models.App.id == a1).first().source_dir = nested
  db.commit()
  assert client.delete(f"/api/apps/{a1}", headers=auth).status_code == 204
  assert os.path.isdir(nested)

  # An immediate-child dir referenced by TWO app rows must survive deleting one.
  shared = os.path.join(data_dir, "apps", "shared-src")
  os.makedirs(shared, exist_ok=True)
  open(os.path.join(shared, "y.json"), "w").close()
  a2 = _make_app(client, owner_token)
  a3 = _make_app(client, owner_token)
  for aid in (a2, a3):
    db.query(models.App).filter(models.App.id == aid).first().source_dir = shared
  db.commit()
  assert client.delete(f"/api/apps/{a2}", headers=auth).status_code == 204
  assert os.path.isdir(shared)  # a3 still references it
