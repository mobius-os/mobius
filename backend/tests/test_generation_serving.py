"""Generation serving: asset attic + request-time resolution + asset-404.

Design item 1 of docs/design/interactive-building-and-chat-robustness.md.
Three coupled guarantees:

- an unreloaded tab still fetches its OLD generation's content-hashed chunks
  from the attic after a dist swap (never 404s its module graph);
- a missing /assets file is a PLAIN 404, never the SPA HTML (a JS module served
  as text/html is MIME-rejected and poisons the cache-first service worker);
- the served build is resolved PER REQUEST, so a dist that appears or goes
  incomplete after boot is served (or floored to baked) with no restart.
"""

import shutil
from pathlib import Path

import pytest

import app.main as main
import app.frontend_watcher as fw


def _reset_memo():
  """Force _resolve_static_dir to recompute (its ~1s memo would otherwise pin
  a stale choice across the deliberate dist mutations these tests make)."""
  main._static_dir_memo["dir"] = None
  main._static_dir_memo["at"] = 0.0


def _write_build(root, marker):
  """Write a complete Vite-shaped build: index.html + a content-hashed chunk."""
  (root / "assets").mkdir(parents=True, exist_ok=True)
  (root / "index.html").write_text(
    f"<!doctype html><html><head><title>{marker}</title></head>"
    f"<body><div id=\"root\"></div></body></html>",
    encoding="utf-8",
  )
  (root / "sw.js").write_text("// service worker", encoding="utf-8")
  (root / "manifest.webmanifest").write_text("{}", encoding="utf-8")
  (root / "assets" / f"index-{marker}.js").write_text(
    f"export const gen = '{marker}';", encoding="utf-8",
  )


@pytest.fixture
def serving():
  """Start each test with no live dist and an empty attic under main's real
  (conftest-tmp) paths; tear everything down and reset the memo after."""
  live = main._live_dir
  attic = main._ATTIC_DIR
  shutil.rmtree(live, ignore_errors=True)
  shutil.rmtree(attic, ignore_errors=True)
  _reset_memo()
  created_baked = []
  try:
    yield {"live": live, "attic": attic, "baked": main._baked_dir,
           "created_baked": created_baked}
  finally:
    shutil.rmtree(live, ignore_errors=True)
    shutil.rmtree(attic, ignore_errors=True)
    for p in created_baked:
      try:
        p.unlink()
      except OSError:
        pass
    # Reset so later tests re-resolve to the baked floor rather than a memo
    # pointing at the dist we just deleted (which would 503 their GET /).
    _reset_memo()


def _spa_active(client):
  r = client.get("/")
  return r.status_code == 200 and "text/html" in r.headers.get(
    "content-type", "",
  )


# -- request-time asset resolution: dist -> attic -> 404 -----------------


def test_old_generation_asset_served_from_attic(client, serving):
  live, attic = serving["live"], serving["attic"]
  _write_build(live, "gen2")  # dist currently serves generation 2
  # generation 1 was atticked when gen2 was published (its chunk left dist).
  (attic / "gen-1" / "assets").mkdir(parents=True)
  (attic / "gen-1" / "assets" / "index-gen1.js").write_text(
    "export const gen = 'gen1';", encoding="utf-8",
  )
  _reset_memo()
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")

  # current generation from dist
  cur = client.get("/assets/index-gen2.js")
  assert cur.status_code == 200
  assert "gen2" in cur.text
  assert "immutable" in cur.headers.get("cache-control", "")
  assert "javascript" in cur.headers.get("content-type", "")

  head = client.head("/assets/index-gen2.js")
  assert head.status_code == 200
  assert head.content == b""
  assert head.headers.get("cache-control") == cur.headers.get("cache-control")
  assert head.headers.get("content-type") == cur.headers.get("content-type")
  assert (
    head.headers.get("x-content-type-options")
    == cur.headers.get("x-content-type-options")
  )

  # old generation from the attic (dist no longer contains it)
  old = client.get("/assets/index-gen1.js")
  assert old.status_code == 200
  assert "gen1" in old.text
  assert "immutable" in old.headers.get("cache-control", "")


def test_unknown_asset_returns_404_not_html(client, serving):
  _write_build(serving["live"], "gen2")
  _reset_memo()
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  for miss in ("/assets/index-doesnotexist.js", "/assets/nope.css",
               "/assets/deep/missing.js"):
    r = client.get(miss)
    assert r.status_code == 404, miss
    assert "text/html" not in r.headers.get("content-type", ""), miss
  head = client.head("/assets/nope.css")
  assert head.status_code == 404
  assert "text/html" not in head.headers.get("content-type", "")


def test_traversal_attempts_rejected(serving):
  live = serving["live"]
  _write_build(live, "gen2")
  (live / "assets" / "sub").mkdir(parents=True, exist_ok=True)
  (live / "assets" / "sub" / "chunk-abcdef.js").write_text("x", encoding="utf-8")
  _reset_memo()
  # legit assets (flat and nested) resolve
  assert main._resolve_asset_file("index-gen2.js") is not None
  assert main._resolve_asset_file("sub/chunk-abcdef.js") is not None
  # escapes are rejected by the containment check, and the empty path (the dir
  # itself) is a miss
  assert main._resolve_asset_file("../index.html") is None
  assert main._resolve_asset_file("../../etc/passwd") is None
  assert main._resolve_asset_file("sub/../../index.html") is None
  assert main._resolve_asset_file("") is None


# -- request-time SPA/static-dir resolution: no restart needed -----------


def test_dist_appearing_after_boot_is_served_without_restart(client, serving):
  # "boot": no live dist -> the baked floor serves.
  _reset_memo()
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  assert main._resolve_static_dir() == main._baked_dir
  boot = client.get("/")
  assert boot.status_code == 200
  assert "AFTERBOOT" not in boot.text

  # a dist appears AFTER boot (agent build) — must be served with no restart.
  _write_build(serving["live"], "AFTERBOOT")
  _reset_memo()
  assert main._resolve_static_dir() == main._live_dir
  live_index = client.get("/")
  assert live_index.status_code == 200
  assert "AFTERBOOT" in live_index.text  # dist index now served live
  live_asset = client.get("/assets/index-AFTERBOOT.js")
  assert live_asset.status_code == 200
  assert "AFTERBOOT" in live_asset.text


def test_baked_floor_served_when_dist_incomplete(client, serving):
  live = serving["live"]
  # An incomplete dist (index.html present, no assets/) must NOT be served —
  # the per-request resolver falls to the baked floor.
  live.mkdir(parents=True, exist_ok=True)
  (live / "index.html").write_text("<title>INCOMPLETE</title>", encoding="utf-8")
  _reset_memo()
  if not _spa_active(client):
    pytest.skip("SPA fallback not registered (no static dir in this env)")
  assert main._resolve_static_dir() == main._baked_dir
  r = client.get("/")
  assert r.status_code == 200
  assert "INCOMPLETE" not in r.text  # the broken dist index is never served
  assert "__mobius-theme__" in r.text  # the baked stub (theme slot) is


# -- frontend_watcher: attic hardlink hook on the dist swap --------------


def _fw_build(root, marker):
  (root / "assets").mkdir(parents=True, exist_ok=True)
  (root / "index.html").write_text(f"<title>{marker}</title>", encoding="utf-8")
  (root / "sw.js").write_text("// service worker", encoding="utf-8")
  (root / "manifest.webmanifest").write_text("{}", encoding="utf-8")
  (root / "assets" / f"index-{marker}.js").write_text(
    f"// {marker}", encoding="utf-8",
  )


@pytest.fixture
def fw_dirs(tmp_path, monkeypatch):
  """Point the watcher's hardcoded /data paths at a throwaway tmp tree."""
  frontend = tmp_path / "frontend"
  frontend.mkdir()
  dirs = {
    "frontend": frontend,
    "dist": frontend / "dist",
    "staging": frontend / ".dist-staging",
    "next": frontend / ".dist-next",
    "old": frontend / ".dist-old",
    "attic": frontend / ".assets-attic",
  }
  monkeypatch.setattr(fw, "_FRONTEND_DIR", frontend)
  monkeypatch.setattr(fw, "_DIST_DIR", dirs["dist"])
  monkeypatch.setattr(fw, "_STAGING_DIST_DIR", dirs["staging"])
  monkeypatch.setattr(fw, "_NEXT_DIST_DIR", dirs["next"])
  monkeypatch.setattr(fw, "_OLD_DIST_DIR", dirs["old"])
  monkeypatch.setattr(fw, "_ATTIC_DIR", dirs["attic"])
  # Global validation has focused publisher coverage; these fixtures exercise
  # only generation rotation and contain deliberately tiny placeholder JS.
  monkeypatch.setattr(fw, "_validate_built_globals", lambda _built: None)
  return dirs


def test_replace_dist_attics_outgoing_generation(fw_dirs):
  dist, nxt, old, attic = (
    fw_dirs["dist"], fw_dirs["next"], fw_dirs["old"], fw_dirs["attic"]
  )
  _fw_build(dist, "g1")  # generation currently served
  _fw_build(nxt, "g2")  # staged next generation
  fw._replace_dist()

  # dist holds the new generation; the old chunk is gone from dist but kept.
  assert (dist / "assets" / "index-g2.js").is_file()
  assert not (dist / "assets" / "index-g1.js").exists()
  assert not old.exists()  # swap scratch cleaned up

  gens = list(attic.glob("gen-*"))
  assert len(gens) == 1
  atticked = gens[0] / "assets" / "index-g1.js"
  assert atticked.is_file()
  assert atticked.read_text() == "// g1"  # survives .dist-old rmtree (hardlink)
  assert (gens[0] / "index.html").is_file()  # index retained per generation


def test_first_build_has_no_outgoing_generation(fw_dirs):
  dist, nxt, attic = fw_dirs["dist"], fw_dirs["next"], fw_dirs["attic"]
  _fw_build(nxt, "g1")  # no existing dist -> nothing to attic
  fw._replace_dist()
  assert (dist / "assets" / "index-g1.js").is_file()
  assert not attic.exists() or list(attic.glob("gen-*")) == []


def test_attic_prunes_to_bounded_rapid_edit_window(fw_dirs):
  dist, nxt, attic = fw_dirs["dist"], fw_dirs["next"], fw_dirs["attic"]
  _fw_build(dist, "g1")
  # One active agent refactor produced four generations in under two minutes;
  # keep a materially deeper but still bounded window for unreloaded tabs.
  for i in range(2, fw._ATTIC_KEEP + 4):
    _fw_build(nxt, f"g{i}")
    fw._replace_dist()

  gens = sorted(attic.glob("gen-*"), key=fw._attic_gen_num)
  assert len(gens) == fw._ATTIC_KEEP
  # The three deliberately excess outgoing generations were pruned; the
  # configured rapid-edit window remains available.
  assert gens[0].name == "gen-3"
  assert gens[-1].name == f"gen-{fw._ATTIC_KEEP + 2}"
  assert (attic / "gen-3" / "assets" / "index-g3.js").is_file()
  assert (
    attic / f"gen-{fw._ATTIC_KEEP + 2}"
    / "assets" / f"index-g{fw._ATTIC_KEEP + 2}.js"
  ).is_file()
  assert not (attic / "gen-1").exists()
  assert not (attic / "gen-2").exists()


def test_publish_reads_only_the_staging_generation(fw_dirs):
  # The source watcher is Vite now; Python publishes only the staging tree.
  # Attic content must never be copied forward into the next live generation.
  dist, staging, attic = (
    fw_dirs["dist"], fw_dirs["staging"], fw_dirs["attic"]
  )
  _fw_build(staging, "g1")
  (attic / "gen-1" / "assets").mkdir(parents=True)
  (attic / "gen-1" / "assets" / "stale.js").write_text(
    "// stale", encoding="utf-8",
  )

  fw._publish_built_dir(staging, "test")

  assert (dist / "assets" / "index-g1.js").is_file()
  assert not (dist / "assets" / "stale.js").exists()
