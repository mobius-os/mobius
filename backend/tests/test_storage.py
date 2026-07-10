"""Storage API: tests for both envelope and inner-object PUT forms."""

import json

import pytest


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


@pytest.mark.asyncio
async def test_read_capped_body_rejects_oversize():
  """read_capped_body refuses a body over the cap with 413 (round-8 #3,
  round-9 #4) — bound the host's memory against a runaway write/upload. Pass an
  explicit small cap and call the helper directly (no PUT, no module-global
  monkeypatch) so it's robust to suite import-order quirks."""
  from fastapi import HTTPException
  from starlette.requests import Request
  from app.storage_io import read_capped_body

  async def receive():
    return {"type": "http.request", "body": b"x" * 100, "more_body": False}

  req = Request(
    {"type": "http", "headers": [(b"content-length", b"100")]}, receive
  )
  with pytest.raises(HTTPException) as exc:
    await read_capped_body(req, cap=16)
  assert exc.value.status_code == 413


def test_large_text_read_roundtrips(client, auth, owner_token):
  """A text file above the inline threshold streams back intact via the
  FileResponse path (round-8 #3)."""
  app_id = _make_app(client, owner_token)
  big = "a" * (300 * 1024)   # > _INLINE_READ_MAX (256 KB) -> streamed
  assert client.put(
    f"/api/storage/apps/{app_id}/big.txt",
    json={"content": big}, headers=auth,
  ).status_code == 204
  got = client.get(f"/api/storage/apps/{app_id}/big.txt", headers=auth)
  assert got.status_code == 200
  assert got.text == big


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
  """A caller-supplied source_dir must stay inside an approved source root."""
  import os
  data_dir = os.environ["DATA_DIR"]
  auth = {"Authorization": f"Bearer {owner_token}"}
  with open(os.path.join(data_dir, "service-token.txt"), encoding="utf-8") as f:
    service_auth = {"Authorization": f"Bearer {f.read()}"}
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
  # Platform core dirs are service-only, only for known built-in slugs, and the
  # app slug must match the core source directory.
  core_dir = os.path.join(data_dir, "platform", "core-apps", "memory")
  os.makedirs(core_dir, exist_ok=True)
  assert client.post("/api/apps/", json={
    "name": "Memory", "description": "x", "jsx_source": jsx,
    "source_dir": core_dir,
  }, headers=auth).status_code == 403
  assert client.post("/api/apps/", json={
    "name": "Memory", "description": "x", "jsx_source": jsx,
    "source_dir": core_dir,
  }, headers=service_auth).status_code == 201
  non_core_dir = os.path.join(data_dir, "platform", "core-apps", "notes")
  os.makedirs(non_core_dir, exist_ok=True)
  assert client.post("/api/apps/", json={
    "name": "Notes", "description": "x", "jsx_source": jsx,
    "source_dir": non_core_dir,
  }, headers=auth).status_code == 400
  mismatch_dir = os.path.join(data_dir, "platform", "core-apps", "reflection")
  os.makedirs(mismatch_dir, exist_ok=True)
  assert client.post("/api/apps/", json={
    "name": "Not Reflection", "description": "x", "jsx_source": jsx,
    "source_dir": mismatch_dir,
  }, headers=auth).status_code == 400


def test_create_rejects_duplicate_source_dir(client, auth, owner_token):
  """Two apps can't claim the same source_dir — sharing a source tree is
  ambiguous for the watcher and forces conservative uninstall cleanup (Codex
  review round-9 #3)."""
  import os
  data_dir = os.environ["DATA_DIR"]
  jsx = "export default function App(){ return <div/> }"
  d = os.path.join(data_dir, "apps", "dup-src")
  assert client.post("/api/apps/", json={
    "name": "dup-a", "description": "x", "jsx_source": jsx, "source_dir": d,
  }, headers=auth).status_code == 201
  r = client.post("/api/apps/", json={
    "name": "dup-b", "description": "x", "jsx_source": jsx, "source_dir": d,
  }, headers=auth)
  assert r.status_code == 409


def test_patch_conflicting_source_dir_keeps_bundle(client, auth, owner_token):
  """A PATCH with new JSX + a source_dir another app claims is rejected 409
  WITHOUT overwriting the live bundle — validation precedes the recompile
  (Codex review round-12)."""
  import os
  data_dir = os.environ["DATA_DIR"]
  jsx = "export default function App(){ return <div>A</div> }"
  da = os.path.join(data_dir, "apps", "patch-a")
  client.post("/api/apps/", json={
    "name": "pa", "description": "x", "jsx_source": jsx, "source_dir": da,
  }, headers=auth)
  db_dir = os.path.join(data_dir, "apps", "patch-b")
  b = client.post("/api/apps/", json={
    "name": "pb", "description": "x", "jsx_source": jsx, "source_dir": db_dir,
  }, headers=auth).json()["id"]
  bundle = os.path.join(data_dir, "compiled", f"app-{b}.js")
  before = open(bundle, "rb").read()
  r = client.patch(f"/api/apps/{b}", json={
    "jsx_source": "export default function App(){ return <div>NEW</div> }",
    "source_dir": da,   # already claimed by app pa -> 409 before compile
  }, headers=auth)
  assert r.status_code == 409
  assert open(bundle, "rb").read() == before   # bundle untouched


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


# Transactional bundle recompile — both PATCH and the file watcher route their
# recompiles through compiler.recompile_app_bundle, which compiles out-of-place
# and swaps the live bundle in only after the DB commit succeeds.


@pytest.mark.asyncio
async def test_recompile_app_bundle_promotes_after_commit(client, owner_token, db):
  """The new bundle goes live only after a successful commit, and the staging
  file is consumed by the atomic swap (not left behind)."""
  import os
  import app.models as models
  from app.compiler import recompile_app_bundle
  app_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  row = db.query(models.App).filter(models.App.id == app_id).first()
  new_jsx = "export default function App(){ return <div>PROMOTED</div> }"
  await recompile_app_bundle(db, row, new_jsx)
  assert "PROMOTED" in open(live, encoding="utf-8").read()
  assert not os.path.exists(live + ".staging")
  assert row.jsx_source == new_jsx


@pytest.mark.asyncio
async def test_recompile_app_bundle_commit_failure_keeps_live_bundle(
  client, owner_token, db,
):
  """A commit failure discards the staging file and leaves the live bundle
  exactly as it was — never a half-applied / uncommitted bundle."""
  import os
  import app.models as models
  from app.compiler import recompile_app_bundle
  app_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  before = open(live, "rb").read()
  row = db.query(models.App).filter(models.App.id == app_id).first()

  class _FailCommit:
    def commit(self):
      raise RuntimeError("commit boom")

    def rollback(self):
      db.rollback()

  new_jsx = "export default function App(){ return <div>CHANGED</div> }"
  with pytest.raises(RuntimeError):
    await recompile_app_bundle(_FailCommit(), row, new_jsx)
  assert open(live, "rb").read() == before
  assert not os.path.exists(live + ".staging")


@pytest.mark.asyncio
async def test_recompile_app_bundle_bad_jsx_keeps_live_bundle(
  client, owner_token, db,
):
  """An esbuild failure leaves the live bundle untouched (esbuild writes its
  outfile only on success) and raises so the caller can roll back."""
  import os
  import app.models as models
  from app.compiler import recompile_app_bundle
  app_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  before = open(live, "rb").read()
  row = db.query(models.App).filter(models.App.id == app_id).first()
  # Has `export default` (passes the cheap guard) but the JSX is unclosed, so
  # esbuild itself fails.
  with pytest.raises(RuntimeError):
    await recompile_app_bundle(
      db, row, "export default function App(){ return <div> }",
    )
  assert open(live, "rb").read() == before


# Startup missing-bundle reconciler — a crash between the install's db.commit()
# and the post-commit os.replace() that promotes the staging bundle leaves an
# App row whose compiled_path was never written (and reap_staging_bundles then
# deletes the only staging copy). The boot reconciler recompiles any live App
# with non-empty jsx_source whose bundle is missing/empty, so the app self-heals
# on the next restart instead of 404ing forever. See feature 109.


@pytest.mark.asyncio
async def test_reconcile_missing_bundles_recompiles_missing_file(
  client, owner_token, db,
):
  """A live App whose compiled bundle file is gone is recompiled from its
  stored jsx_source, exactly as a crash-interrupted install would need."""
  import os
  import app.models as models
  from app.compiler import reconcile_missing_bundles
  app_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  # Simulate the crash window: the row exists with jsx_source + compiled_path,
  # but the bundle file the post-commit os.replace would have written is absent.
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.jsx_source = "export default function App(){ return <div>HEALED</div> }"
  db.commit()
  os.remove(live)
  assert not os.path.exists(live)

  healed = await reconcile_missing_bundles(db)

  assert app_id in healed
  assert "HEALED" in open(live, encoding="utf-8").read()


@pytest.mark.asyncio
async def test_reconcile_missing_bundles_recompiles_empty_file(
  client, owner_token, db,
):
  """A zero-byte bundle (a partial/aborted write) is treated as missing and
  recompiled, not served as an empty module."""
  import os
  import app.models as models
  from app.compiler import reconcile_missing_bundles
  app_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  open(live, "w").close()
  assert os.path.getsize(live) == 0

  await reconcile_missing_bundles(db)

  assert os.path.getsize(live) > 0


@pytest.mark.asyncio
async def test_reconcile_missing_bundles_skips_healthy_and_tombstoned(
  client, owner_token, db,
):
  """The sweep recompiles nothing it shouldn't: a healthy app keeps its bundle
  untouched, and a tombstoned (uninstalled) app is not resurrected."""
  import os
  import app.models as models
  from app.compiler import reconcile_missing_bundles
  healthy_id = _make_app(client, owner_token)
  tombstoned_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  healthy = os.path.join(data_dir, "compiled", f"app-{healthy_id}.js")
  tombstoned = os.path.join(data_dir, "compiled", f"app-{tombstoned_id}.js")
  before = open(healthy, "rb").read()
  # Tombstone the second app and delete its bundle — a uninstalled row must not
  # be recompiled back to life.
  row = db.query(models.App).filter(models.App.id == tombstoned_id).first()
  row.deleted_at = __import__("app.timeutil", fromlist=["now_naive_utc"]).now_naive_utc()
  db.commit()
  os.remove(tombstoned)

  healed = await reconcile_missing_bundles(db)

  assert healthy_id not in healed
  assert tombstoned_id not in healed
  assert open(healthy, "rb").read() == before
  assert not os.path.exists(tombstoned)


@pytest.mark.asyncio
async def test_reconcile_missing_bundles_isolates_one_apps_failure(
  client, owner_token, db,
):
  """One app whose jsx_source no longer compiles must not block the sweep from
  healing the others — a compile error is logged and skipped, not fatal."""
  import os
  import app.models as models
  from app.compiler import reconcile_missing_bundles
  broken_id = _make_app(client, owner_token)
  good_id = _make_app(client, owner_token)
  data_dir = os.environ["DATA_DIR"]
  broken = os.path.join(data_dir, "compiled", f"app-{broken_id}.js")
  good = os.path.join(data_dir, "compiled", f"app-{good_id}.js")
  broken_row = db.query(models.App).filter(models.App.id == broken_id).first()
  broken_row.jsx_source = "export default function App(){ return <div> }"
  db.commit()
  os.remove(broken)
  os.remove(good)

  healed = await reconcile_missing_bundles(db)

  assert good_id in healed
  assert broken_id not in healed
  assert os.path.getsize(good) > 0
  assert not os.path.exists(broken)


def test_create_app_compiles_relative_source_import(client, owner_token):
  """A real source_dir compile lets index.jsx import sibling modules."""
  import os
  data_dir = os.environ["DATA_DIR"]
  src = os.path.join(data_dir, "apps", "modular-create")
  os.makedirs(src, exist_ok=True)
  with open(os.path.join(src, "Widget.jsx"), "w", encoding="utf-8") as f:
    f.write("export function Widget(){ return <span>MODULAR_WIDGET</span> }")
  jsx = (
    "import { Widget } from './Widget.jsx';\n"
    "export default function App(){ return <Widget /> }"
  )
  r = client.post("/api/apps/", json={
    "name": "modular-create",
    "description": "x",
    "jsx_source": jsx,
    "source_dir": src,
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  assert "MODULAR_WIDGET" in open(live, encoding="utf-8").read()
  assert open(os.path.join(src, "index.jsx"), encoding="utf-8").read() == jsx


@pytest.mark.asyncio
async def test_watcher_recompiles_registered_app(client, owner_token):
  """An on-disk source edit, resolved to its app by source_dir, recompiles the
  live bundle through the locked transactional path."""
  import asyncio
  import os
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal
  data_dir = os.environ["DATA_DIR"]
  src = os.path.join(data_dir, "apps", "watch-me")
  os.makedirs(src, exist_ok=True)
  app_id = client.post("/api/apps/", json={
    "name": "watchme", "description": "x",
    "jsx_source": "export default function App(){ return <div>V0</div> }",
    "source_dir": src,
  }, headers={"Authorization": f"Bearer {owner_token}"}).json()["id"]
  jsx_path = os.path.join(src, "index.jsx")
  new_jsx = "export default function App(){ return <div>V1</div> }"
  with open(jsx_path, "w", encoding="utf-8") as f:
    f.write(new_jsx)
  await _JsxHandler(asyncio.get_running_loop())._recompile(jsx_path)
  s = SessionLocal()
  try:
    row = s.query(models.App).filter(models.App.id == app_id).first()
    assert row.jsx_source == new_jsx
  finally:
    s.close()
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  assert "V1" in open(live, encoding="utf-8").read()


@pytest.mark.asyncio
async def test_watcher_recompiles_when_imported_module_changes(
  client, owner_token,
):
  """A sibling source edit rebuilds even when index.jsx bytes are unchanged."""
  import asyncio
  import os
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal
  data_dir = os.environ["DATA_DIR"]
  src = os.path.join(data_dir, "apps", "watch-module")
  os.makedirs(src, exist_ok=True)
  component = os.path.join(src, "Widget.jsx")
  with open(component, "w", encoding="utf-8") as f:
    f.write("export function Widget(){ return <span>MODULE_V0</span> }")
  jsx = (
    "import { Widget } from './Widget.jsx';\n"
    "export default function App(){ return <Widget /> }"
  )
  app_id = client.post("/api/apps/", json={
    "name": "watch-module", "description": "x",
    "jsx_source": jsx,
    "source_dir": src,
  }, headers={"Authorization": f"Bearer {owner_token}"}).json()["id"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  assert "MODULE_V0" in open(live, encoding="utf-8").read()

  with open(component, "w", encoding="utf-8") as f:
    f.write("export function Widget(){ return <span>MODULE_V1</span> }")
  await _JsxHandler(asyncio.get_running_loop())._recompile(component)

  s = SessionLocal()
  try:
    row = s.query(models.App).filter(models.App.id == app_id).first()
    assert row.jsx_source == jsx
  finally:
    s.close()
  compiled = open(live, encoding="utf-8").read()
  assert "MODULE_V1" in compiled
  assert "MODULE_V0" not in compiled


@pytest.mark.asyncio
async def test_watcher_recompiles_platform_core_source(
  client, owner_token,
):
  """A platform core source edit recompiles the app registered to that dir."""
  import asyncio
  import os
  import app.models as models
  from app.app_watcher import _JsxHandler
  from app.database import SessionLocal
  data_dir = os.environ["DATA_DIR"]
  with open(os.path.join(data_dir, "service-token.txt"), encoding="utf-8") as f:
    service_auth = {"Authorization": f"Bearer {f.read()}"}
  src = os.path.join(data_dir, "platform", "core-apps", "beat-machine")
  os.makedirs(src, exist_ok=True)
  component = os.path.join(src, "Widget.jsx")
  with open(component, "w", encoding="utf-8") as f:
    f.write("export function Widget(){ return <span>CORE_V0</span> }")
  jsx = (
    "import { Widget } from './Widget.jsx';\n"
    "export default function App(){ return <Widget /> }"
  )
  app_id = client.post("/api/apps/", json={
    "name": "Beat Machine", "description": "x",
    "jsx_source": jsx, "source_dir": src,
  }, headers=service_auth).json()["id"]
  live = os.path.join(data_dir, "compiled", f"app-{app_id}.js")
  assert "CORE_V0" in open(live, encoding="utf-8").read()

  with open(component, "w", encoding="utf-8") as f:
    f.write("export function Widget(){ return <span>CORE_V1</span> }")
  await _JsxHandler(asyncio.get_running_loop())._recompile(component)

  s = SessionLocal()
  try:
    row = s.query(models.App).filter(models.App.id == app_id).first()
    assert row.source_dir == src
  finally:
    s.close()
  compiled = open(live, encoding="utf-8").read()
  assert "CORE_V1" in compiled
  assert "CORE_V0" not in compiled


@pytest.mark.asyncio
async def test_watcher_debounce_keeps_module_rebuild_signal():
  """A later index event must not erase an earlier module-change rebuild."""
  import asyncio
  import os
  from app.app_watcher import _JsxHandler
  data_dir = os.environ["DATA_DIR"]
  src = os.path.join(data_dir, "apps", "debounce-module")
  index_path = os.path.join(src, "index.jsx")
  module_path = os.path.join(src, "Widget.jsx")
  handler = _JsxHandler(asyncio.get_running_loop())
  await handler._reschedule(src, module_path, True)
  await handler._reschedule(src, index_path, False)
  try:
    _, force_rebuild = handler._pending[src]
    assert force_rebuild is True
  finally:
    handler.close()


@pytest.mark.asyncio
async def test_watcher_skips_unclaimed_source_dir():
  """A source file in a directory no app row claims is a no-op (the create-then-
  register gap) — the watcher must not crash or guess an owner."""
  import asyncio
  import os
  from app.app_watcher import _JsxHandler
  data_dir = os.environ["DATA_DIR"]
  orphan = os.path.join(data_dir, "apps", "orphan-dir")
  os.makedirs(orphan, exist_ok=True)
  jsx_path = os.path.join(orphan, "index.jsx")
  with open(jsx_path, "w", encoding="utf-8") as f:
    f.write("export default function App(){ return <div/> }")
  # Must return without raising (and without compiling anything).
  await _JsxHandler(asyncio.get_running_loop())._recompile(jsx_path)


def test_create_app_leaves_no_staging_bundle(client, owner_token):
  """create_app compiles through the same staging swap — the live bundle exists
  and no .staging artifact is left behind once the swap completes."""
  import os
  app_id = _make_app(client, owner_token)
  live = os.path.join(os.environ["DATA_DIR"], "compiled", f"app-{app_id}.js")
  assert os.path.exists(live)
  assert not os.path.exists(live + ".staging")


# -- move (rename) route -------------------------------------------------


def test_move_renames_file(client, auth, owner_token):
  """A move relocates a file; the source is gone and the dest has the body."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/old.json", json={"k": 1}, headers=auth,
  )
  r = client.post(
    f"/api/storage/apps/{app_id}/move",
    json={"from": "old.json", "to": "renamed.json"},
    headers=auth,
  )
  assert r.status_code == 204
  assert client.get(
    f"/api/storage/apps/{app_id}/old.json", headers=auth
  ).status_code == 404
  assert client.get(
    f"/api/storage/apps/{app_id}/renamed.json", headers=auth
  ).json() == {"k": 1}


def test_move_into_new_folder_creates_parents(client, auth, owner_token):
  """Moving into a not-yet-existing folder creates the parent dirs."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/note.json", json={"k": 1}, headers=auth,
  )
  r = client.post(
    f"/api/storage/apps/{app_id}/move",
    json={"from": "note.json", "to": "archive/2026/note.json"},
    headers=auth,
  )
  assert r.status_code == 204
  assert client.get(
    f"/api/storage/apps/{app_id}/archive/2026/note.json", headers=auth
  ).json() == {"k": 1}


def test_move_missing_source_404(client, auth, owner_token):
  """A move whose source doesn't exist is a clean 404."""
  app_id = _make_app(client, owner_token)
  r = client.post(
    f"/api/storage/apps/{app_id}/move",
    json={"from": "nope.json", "to": "x.json"},
    headers=auth,
  )
  assert r.status_code == 404


def test_move_existing_destination_409(client, auth, owner_token):
  """A move onto an existing destination is rejected, not a silent clobber."""
  app_id = _make_app(client, owner_token)
  client.put(f"/api/storage/apps/{app_id}/a.json", json={"k": 1}, headers=auth)
  client.put(f"/api/storage/apps/{app_id}/b.json", json={"k": 2}, headers=auth)
  r = client.post(
    f"/api/storage/apps/{app_id}/move",
    json={"from": "a.json", "to": "b.json"},
    headers=auth,
  )
  assert r.status_code == 409
  # Both files survive untouched.
  assert client.get(
    f"/api/storage/apps/{app_id}/a.json", headers=auth
  ).json() == {"k": 1}
  assert client.get(
    f"/api/storage/apps/{app_id}/b.json", headers=auth
  ).json() == {"k": 2}


def test_move_rejects_traversal(client, auth, owner_token):
  """A move whose `to` escapes the app tree via `..` is rejected with 400.

  The destination would resolve outside /data/apps/<id>; `_resolve`'s
  traversal + containment check rejects it before any file is touched, so
  the move can't be used to write into a sibling app or the host fs.
  """
  app_id = _make_app(client, owner_token)
  client.put(f"/api/storage/apps/{app_id}/x.json", json={"k": 1}, headers=auth)
  r = client.post(
    f"/api/storage/apps/{app_id}/move",
    json={"from": "x.json", "to": "../../escape.json"},
    headers=auth,
  )
  assert r.status_code == 400
  # The source is untouched by the rejected move.
  assert client.get(
    f"/api/storage/apps/{app_id}/x.json", headers=auth
  ).json() == {"k": 1}


def test_move_rejects_traversal_in_source(client, auth, owner_token):
  """A move whose `from` escapes the app tree via `..` is rejected with 400."""
  app_id = _make_app(client, owner_token)
  r = client.post(
    f"/api/storage/apps/{app_id}/move",
    json={"from": "../../etc/hostname", "to": "stolen.txt"},
    headers=auth,
  )
  assert r.status_code == 400


# -- recursive folder delete --------------------------------------------


def test_delete_folder_removes_contents(client, auth, owner_token):
  """Deleting a folder removes it and every file beneath it."""
  app_id = _make_app(client, owner_token)
  client.put(
    f"/api/storage/apps/{app_id}/docs/a.json", json={"k": 1}, headers=auth,
  )
  client.put(
    f"/api/storage/apps/{app_id}/docs/sub/b.json", json={"k": 2}, headers=auth,
  )
  r = client.delete(f"/api/storage/apps/{app_id}/folder/docs", headers=auth)
  assert r.status_code == 204
  # Everything under docs/ is gone.
  assert client.get(
    f"/api/storage/apps/{app_id}/docs/a.json", headers=auth
  ).status_code == 404
  assert client.get(
    f"/api/storage/apps/{app_id}/docs/sub/b.json", headers=auth
  ).status_code == 404
  # The folder itself no longer lists.
  listing = client.get(
    f"/api/storage/apps-list/{app_id}/", headers=auth
  ).json()
  assert all(e["name"] != "docs" for e in listing["entries"])


def test_delete_folder_missing_404(client, auth, owner_token):
  """Deleting a folder that doesn't exist is a clean 404."""
  app_id = _make_app(client, owner_token)
  r = client.delete(f"/api/storage/apps/{app_id}/folder/nope", headers=auth)
  assert r.status_code == 404


def test_delete_folder_rejects_file_path(client, auth, owner_token):
  """The recursive route refuses a FILE path (use the per-file DELETE)."""
  app_id = _make_app(client, owner_token)
  client.put(f"/api/storage/apps/{app_id}/file.json", json={"k": 1}, headers=auth)
  r = client.delete(f"/api/storage/apps/{app_id}/folder/file.json", headers=auth)
  assert r.status_code == 400
  # The file survives the rejected folder-delete.
  assert client.get(
    f"/api/storage/apps/{app_id}/file.json", headers=auth
  ).json() == {"k": 1}


def test_delete_folder_rejects_traversal(client, auth, owner_token):
  """A recursive delete with a `..` path is rejected with 400 — no rmtree
  outside the app's own storage tree.

  The `..` segments are percent-encoded so the HTTP client doesn't
  normalize them away before the request leaves; the server decodes them
  back to literal `..` parts, which `_resolve` rejects.
  """
  app_id = _make_app(client, owner_token)
  r = client.delete(
    f"/api/storage/apps/{app_id}/folder/%2e%2e/%2e%2e/etc", headers=auth,
  )
  assert r.status_code == 400



def test_app_storage_version_header_is_gated(client, auth, owner_token):
  app_id = _make_app(client, owner_token)
  assert client.put(
    f"/api/storage/apps/{app_id}/cas.json", json={"v": 1}, headers=auth,
  ).status_code == 204

  plain = client.get(f"/api/storage/apps/{app_id}/cas.json", headers=auth)
  assert plain.status_code == 200
  assert "etag" not in {k.lower() for k in plain.headers.keys()}

  versioned = client.get(
    f"/api/storage/apps/{app_id}/cas.json",
    headers={**auth, "X-Mobius-Version": "1"},
  )
  assert versioned.status_code == 200
  assert versioned.headers.get("etag")


def test_app_storage_if_match_conflict_412_and_unconditional_put_unchanged(client, auth, owner_token):
  app_id = _make_app(client, owner_token)
  path = f"/api/storage/apps/{app_id}/cas-write.json"
  assert client.put(path, json={"v": 1}, headers=auth).status_code == 204
  version = client.get(path, headers={**auth, "X-Mobius-Version": "1"}).headers["etag"]

  ok = client.put(path, json={"v": 2}, headers={**auth, "If-Match": version})
  assert ok.status_code == 204
  assert ok.headers.get("etag")

  stale = client.put(path, json={"v": 3}, headers={**auth, "If-Match": version})
  assert stale.status_code == 412
  assert client.get(path, headers=auth).json() == {"v": 2}

  unconditional = client.put(path, json={"v": 4}, headers=auth)
  assert unconditional.status_code == 204
  assert "etag" not in {k.lower() for k in unconditional.headers.keys()}
  assert client.get(path, headers=auth).json() == {"v": 4}


def test_app_storage_if_match_tolerates_proxy_encoding_suffix(client, auth, owner_token):
  # A transcoding reverse proxy (Caddy `encode`) rewrites the ETag it forwards
  # so the tag stays unique per content-encoding — a strong `"<tok>"` becomes
  # `"<tok>-gzip"`. The client echoes that suffixed tag back in If-Match, so a
  # strong string compare would 412 every CAS write to a resource whose GET got
  # compressed. The server must strip the suffix and still match.
  app_id = _make_app(client, owner_token)
  path = f"/api/storage/apps/{app_id}/proxy-cas.json"
  assert client.put(path, json={"v": 1}, headers=auth).status_code == 204
  version = client.get(path, headers={**auth, "X-Mobius-Version": "1"}).headers["etag"]

  gzip_tag = version[:-1] + '-gzip"'
  ok = client.put(path, json={"v": 2}, headers={**auth, "If-Match": gzip_tag})
  assert ok.status_code == 204
  assert client.get(path, headers=auth).json() == {"v": 2}

  # `If-Match: *` means "any current representation exists" (RFC 9110) — the file
  # is present, so the write proceeds.
  assert client.put(
    path, json={"v": 3}, headers={**auth, "If-Match": "*"}
  ).status_code == 204
  assert client.get(path, headers=auth).json() == {"v": 3}

  # A weak validator never strong-matches (If-Match is a strong comparison), and
  # a genuinely stale tag conflicts — the normalization must not over-match.
  fresh = client.get(path, headers={**auth, "X-Mobius-Version": "1"}).headers["etag"]
  assert client.put(
    path, json={"v": 9}, headers={**auth, "If-Match": "W/" + fresh}
  ).status_code == 412
  assert client.put(
    path, json={"v": 9}, headers={**auth, "If-Match": '"deadbeef-1-gzip"'}
  ).status_code == 412
  assert client.get(path, headers=auth).json() == {"v": 3}


def test_etag_matches_normalizes_proxy_transforms():
  from app.storage_io import etag_matches

  token = '"18be2bdc61e90a77-660"'
  assert etag_matches(token, token)
  assert etag_matches(token, '"18be2bdc61e90a77-660-gzip"')
  assert etag_matches(token, '"18be2bdc61e90a77-660-br"')
  assert etag_matches(token, '  "18be2bdc61e90a77-660-gzip"  ')
  assert etag_matches(token, "*")  # RFC: matches any existing representation
  assert etag_matches(token, f'"stale-0", {token[:-1]}-zstd"')  # comma, 2nd matches
  assert not etag_matches(token, 'W/"18be2bdc61e90a77-660"')  # weak never matches
  assert not etag_matches(token, 'W/"18be2bdc61e90a77-660-gzip"')  # weak + suffix
  assert not etag_matches(token, '"18be2bdc61e90a77-661"')
  assert not etag_matches(token, '"18be2bdc61e90a77-660-lol"')


def test_app_storage_if_none_match_create_if_absent(client, auth, owner_token):
  app_id = _make_app(client, owner_token)
  path = f"/api/storage/apps/{app_id}/created-once.json"

  first = client.put(path, json={"v": 1}, headers={**auth, "If-None-Match": "*"})
  assert first.status_code == 204
  assert first.headers.get("etag")

  second = client.put(path, json={"v": 2}, headers={**auth, "If-None-Match": "*"})
  assert second.status_code == 412
  assert client.get(path, headers=auth).json() == {"v": 1}
