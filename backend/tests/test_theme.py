import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-exactly-32-chars!!")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mobius_test/test.db")
os.environ.setdefault("DATA_DIR", "/tmp/mobius_test")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:5173")

from app.theme import (
  inject_theme_into_html,
  extract_imports,
  _ensure_core_vars,
  snapshot_theme_if_present,
  reset_theme_override,
)


def test_extract_imports_splits_imports_from_css():
  css = (
    "@import url('https://fonts.googleapis.com/css2?family=Poppins');\n"
    "@import url(\"https://fonts.googleapis.com/css2?family=Fira+Code\");\n"
    ":root { --font: 'Poppins', sans-serif; }\n"
  )
  imports, remaining = extract_imports(css)
  assert imports == [
    "https://fonts.googleapis.com/css2?family=Poppins",
    "https://fonts.googleapis.com/css2?family=Fira+Code",
  ]
  assert "@import" not in remaining
  assert "--font" in remaining


def test_extract_imports_no_imports():
  css = ":root { --bg: #fff; }"
  imports, remaining = extract_imports(css)
  assert imports == []
  assert remaining == css


def test_inject_theme_adds_link_tags_for_imports(tmp_path):
  theme_css = (
    "@import url('https://fonts.googleapis.com/css2?family=Poppins');\n"
    ":root { --font: 'Poppins', sans-serif; }\n"
  )
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(theme_css)

  html = '<html><head><title>Test</title></head><body style="margin:0;background:#0c0f14"></body></html>'
  result = inject_theme_into_html(html, str(tmp_path))

  assert '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins">' in result
  assert "@import" not in result
  assert "--font" in result


def test_ensure_core_vars_injects_missing():
  """If the theme defines only some variables, missing core
  variables get filled in from DEFAULT_THEME so the shell can't
  fall back to an invisible hardcoded literal like #111."""
  css = ":root { --accent: #ff00aa; }"
  out = _ensure_core_vars(css)
  # Original kept verbatim.
  assert "--accent: #ff00aa" in out
  # Missing variables injected.
  assert "--bg:" in out
  assert "--text:" in out
  assert "--surface:" in out


def test_ensure_core_vars_skips_when_all_present():
  """A complete theme stays byte-identical."""
  css = """\
:root {
  --bg: #000;
  --surface: #111;
  --surface2: #222;
  --text: #fff;
  --muted: #999;
  --accent: #f0f;
  --accent-hover: #faf;
  --accent-dim: rgba(255, 0, 255, 0.1);
  --border: #333;
  --border-light: #444;
  --danger: #f00;
  --green: #0f0;
  --font: sans-serif;
  --mono: monospace;
}
"""
  assert _ensure_core_vars(css) == css


def test_ensure_core_vars_leaves_creative_css_alone():
  """Anything other than variable injection is a no-op: blur,
  translucent surfaces, fixed-position overlays, global focus
  rules, animations — all preserved. The agent has creative
  freedom; readability is a documentation concern, not a
  server-side rewrite."""
  css = """\
:root {
  --bg: #082015;
  --surface: rgba(20, 60, 38, 0.78);
  --surface2: rgba(28, 78, 50, 0.88);
  --text: #f0e6c8;
  --muted: #9bb3a3;
  --accent: #d4a437;
  --accent-hover: #f0c451;
  --accent-dim: rgba(212, 164, 55, 0.14);
  --border: #2d6e47;
  --border-light: #1a4d2e;
  --danger: #c4554e;
  --green: #1a8c4a;
  --font: 'Inter', sans-serif;
  --mono: monospace;
}
.sidenav { backdrop-filter: blur(8px); }
.hero { filter: blur(4px); }
body::before { position: fixed; inset: 0; z-index: 10; }
input:focus-visible { outline: 2px solid red; }
"""
  out = _ensure_core_vars(css)
  # Output is byte-identical because all core vars are defined.
  assert out == css


def test_inject_theme_no_imports_no_link_tags(tmp_path):
  theme_css = ":root { --bg: #1a1a1a; }"
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(theme_css)

  html = '<html><head><title>Test</title></head><body style="margin:0;background:#0c0f14"></body></html>'
  result = inject_theme_into_html(html, str(tmp_path))

  assert '<link rel="stylesheet"' not in result
  assert "--bg" in result


# /api/theme endpoint tests --------------------------------------------------
# The endpoint returns the *effective* theme — user override if present,
# DEFAULT_THEME otherwise. Lets the agent reset to defaults via DELETE on
# the storage URL without writing a complete default block in JS.

def test_api_theme_returns_default_when_no_override(client, auth):
  """No theme.css → endpoint returns DEFAULT_THEME and default --bg."""
  from app.theme import DEFAULT_THEME
  res = client.get("/api/theme", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["css"] == DEFAULT_THEME
  # DEFAULT_THEME has --bg: #0d0d0d (neutralized 2026-05).
  assert body["bg"] == "#0d0d0d"


def test_api_theme_returns_user_override_when_present(client, auth):
  """User-written theme.css → endpoint returns it verbatim."""
  import os
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  custom = ":root { --bg: #ff0000; --accent: #00ff00; }"
  with open(os.path.join(shared, "theme.css"), "w") as f:
    f.write(custom)
  try:
    res = client.get("/api/theme", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["css"] == custom
    assert body["bg"] == "#ff0000"
  finally:
    os.remove(os.path.join(shared, "theme.css"))


def test_api_theme_returns_default_when_override_is_empty(client, auth):
  """Empty theme.css → endpoint falls back to DEFAULT_THEME."""
  import os
  from app.theme import DEFAULT_THEME
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  with open(os.path.join(shared, "theme.css"), "w") as f:
    f.write("")
  try:
    res = client.get("/api/theme", headers=auth)
    assert res.status_code == 200
    assert res.json()["css"] == DEFAULT_THEME
  finally:
    os.remove(os.path.join(shared, "theme.css"))


def test_api_theme_reset_via_delete(client, auth):
  """Agent's reset path: DELETE the storage URL → /api/theme returns
  defaults. End-to-end verification that the architecture works."""
  from app.theme import DEFAULT_THEME

  # Set a custom theme via the storage API.
  custom = ":root { --bg: #123456; }"
  res = client.put(
    "/api/storage/shared/theme.css",
    headers=auth,
    json={"content": custom},
  )
  assert res.status_code in (200, 201, 204)

  # Verify endpoint returns the override.
  body = client.get("/api/theme", headers=auth).json()
  assert body["css"] == custom

  # Reset by deleting.
  res = client.delete("/api/storage/shared/theme.css", headers=auth)
  assert res.status_code in (200, 204)

  # Endpoint now returns defaults.
  body = client.get("/api/theme", headers=auth).json()
  assert body["css"] == DEFAULT_THEME


def test_api_theme_requires_auth(client):
  """Unauth requests return 401 — theme isn't a public endpoint."""
  res = client.get("/api/theme")
  assert res.status_code == 401


# Security tests for inject_theme_into_html ---------------------------------

def test_inject_theme_escapes_style_breakout(tmp_path):
  """A theme.css with `</style><script>...` cannot break out of the
  <style> block. The HTML parser ends a style block on the first
  literal `</`; any user-controlled CSS containing `</style>` would
  otherwise produce sibling tags in the head, allowing script
  injection. We escape `</` to `<\\/` which the CSS parser ignores
  but the HTML parser doesn't recognize as a closing tag.

  Stored-XSS regression guard for owner-controlled theme CSS.
  """
  malicious = "</style><script>window.__pwned=1</script><style>"
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(malicious)

  html = '<html><head></head><body style="margin:0;background:#0c0f14"></body></html>'
  result = inject_theme_into_html(html, str(tmp_path))
  head = result.split("</head>")[0]

  # The only `</style>` in the head must be our own wrapper close —
  # anything else means the user-controlled CSS broke out.
  closes = head.count("</style>")
  assert closes == 1, f"unexpected </style> count in head: {closes}"
  # No `</script>` either: verifies the secondary close doesn't appear
  # outside <style>. (`<script>` text inside <style> is inert.)
  assert "</script>" not in head
  # Crucial: parse the head as HTML and verify no <script> tag exists.
  # Use stdlib HTMLParser; if the parser sees a <script> as a real
  # tag in the head, that's a breakout.
  from html.parser import HTMLParser

  class TagFinder(HTMLParser):
    def __init__(self):
      super().__init__()
      self.tags = []

    def handle_starttag(self, tag, attrs):
      self.tags.append(tag)

  parser = TagFinder()
  parser.feed(head + "</head>")
  assert "script" not in parser.tags, f"script tag injected: {parser.tags}"


def test_inject_theme_filters_unsafe_import_urls(tmp_path):
  """@import url('javascript:...') and data: URIs must not produce a
  <link> tag in the rendered HTML. http(s) only."""
  hostile = (
    "@import url('javascript:alert(1)');\n"
    "@import url('data:text/css,body{}');\n"
    "@import url('https://fonts.googleapis.com/css?family=Inter');\n"
    ":root { --bg: #1a1a1a; }\n"
  )
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(hostile)

  html = '<html><head></head><body style="margin:0;background:#0c0f14"></body></html>'
  result = inject_theme_into_html(html, str(tmp_path))

  # No <link> tag pointing at a non-http(s) URL.
  assert 'href="javascript:' not in result
  assert "href='javascript:" not in result
  assert 'href="data:' not in result
  assert "href='data:" not in result
  # The legitimate https URL DOES produce a <link> tag.
  assert 'fonts.googleapis.com' in result


def test_inject_theme_quotes_in_import_urls_dont_inject_attrs(tmp_path):
  """A `"` in a font URL must not break out of the <link href="..."> attr.
  Even if our regex captures part of the URL, html.escape() with
  quote=True ensures the value is attribute-safe."""
  # Construct a URL that includes a literal `"` followed by attribute-
  # injection-shaped text. The `extract_imports` regex's `[^'"]+` may
  # truncate such URLs — verifying the behavior either way.
  tricky = (
    "@import url('https://example.com/x.css?\"onload=alert(1)');\n"
    ":root { --bg: #abcdef; }\n"
  )
  shared = tmp_path / "shared"
  shared.mkdir()
  (shared / "theme.css").write_text(tricky)

  html = '<html><head></head><body style="margin:0;background:#0c0f14"></body></html>'
  result = inject_theme_into_html(html, str(tmp_path))

  # Critical property: any <link> tag's href attribute is properly
  # quoted. Parse the head and check.
  from html.parser import HTMLParser

  class LinkAttrChecker(HTMLParser):
    def __init__(self):
      super().__init__()
      self.bad = False

    def handle_starttag(self, tag, attrs):
      if tag == "link":
        # Any attr name not in the expected set means injection.
        for name, _ in attrs:
          if name not in ("rel", "href"):
            self.bad = True

  parser = LinkAttrChecker()
  parser.feed(result.split("</head>")[0] + "</head>")
  assert not parser.bad, "extra attributes injected into <link>"


# Recovery affordance tests ------------------------------------------------
# Three changes ship together: auto-snapshot on theme write,
# `reset_theme_override` helper, and `POST /api/theme/reset`. Each
# is verified independently so a regression in one doesn't mask
# breakage in another.

def test_snapshot_theme_creates_backup_when_present(tmp_path):
  """An existing theme.css produces a theme.css.bak-<ts> sibling."""
  shared = tmp_path / "shared"
  shared.mkdir()
  src = shared / "theme.css"
  src.write_text(":root { --bg: #ff0000; }")
  backup = snapshot_theme_if_present(str(tmp_path))
  assert backup is not None
  bp = type(src)(backup)
  assert bp.exists()
  assert bp.name.startswith("theme.css.bak-")
  # Backup contains the same bytes as the source pre-snapshot.
  assert bp.read_text() == ":root { --bg: #ff0000; }"
  # Source is left untouched — snapshot is copy, not move.
  assert src.exists()
  assert src.read_text() == ":root { --bg: #ff0000; }"


def test_snapshot_theme_no_op_when_missing(tmp_path):
  """A missing theme.css returns None without raising."""
  (tmp_path / "shared").mkdir()
  assert snapshot_theme_if_present(str(tmp_path)) is None


def test_reset_theme_renames_to_reset_bak(tmp_path):
  """reset_theme_override moves theme.css aside, returns the backup path."""
  shared = tmp_path / "shared"
  shared.mkdir()
  src = shared / "theme.css"
  src.write_text(":root { --bg: #00ff00; }")
  result = reset_theme_override(str(tmp_path))
  assert result["reset"] is True
  backup_path = type(src)(result["backup"])
  assert backup_path.exists()
  assert backup_path.name.startswith("theme.css.reset-bak-")
  assert backup_path.read_text() == ":root { --bg: #00ff00; }"
  # Source is gone — DEFAULT_THEME would paint on next read.
  assert not src.exists()


def test_reset_theme_idempotent_when_no_override(tmp_path):
  """reset_theme_override with no theme.css reports no-op."""
  (tmp_path / "shared").mkdir()
  result = reset_theme_override(str(tmp_path))
  assert result == {"reset": False, "reason": "no override"}


def test_api_theme_reset_endpoint_idempotent(client, auth):
  """POST /api/theme/reset with no override returns reset=False."""
  res = client.post("/api/theme/reset", headers=auth)
  assert res.status_code == 200
  body = res.json()
  assert body["reset"] is False
  assert "reason" in body


def test_api_theme_reset_endpoint_creates_backup(client, auth):
  """POST /api/theme/reset moves the override to a reset-bak file
  and subsequent GET /api/theme returns DEFAULT_THEME."""
  import os
  from app.theme import DEFAULT_THEME
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  custom = ":root { --bg: #abcdef; }"
  with open(os.path.join(shared, "theme.css"), "w") as f:
    f.write(custom)
  try:
    res = client.post("/api/theme/reset", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["reset"] is True
    assert "reset-bak-" in body["backup"]
    # Backup file actually exists on disk.
    assert os.path.exists(body["backup"])
    # The override is gone — GET /api/theme falls back to defaults.
    assert client.get("/api/theme", headers=auth).json()["css"] == DEFAULT_THEME
  finally:
    # Cleanup: remove backup + any stale source.
    for entry in os.listdir(shared):
      if entry.startswith("theme.css"):
        os.remove(os.path.join(shared, entry))


def test_api_theme_reset_requires_auth(client):
  """Unauth POST to /api/theme/reset returns 401."""
  res = client.post("/api/theme/reset")
  assert res.status_code == 401


def test_storage_write_theme_css_auto_snapshots(client, auth):
  """Writing to /api/storage/shared/theme.css snapshots the prior
  version automatically. First write has no prior — no backup; the
  SECOND write produces theme.css.bak-<ts>."""
  import os
  data_dir = os.environ["DATA_DIR"]
  shared = os.path.join(data_dir, "shared")
  os.makedirs(shared, exist_ok=True)
  # Clear any leftover bak files from prior tests.
  for entry in list(os.listdir(shared)):
    if entry.startswith("theme.css"):
      os.remove(os.path.join(shared, entry))
  try:
    # First write: no prior, no backup expected.
    res = client.put(
      "/api/storage/shared/theme.css",
      headers=auth,
      json={"content": ":root { --bg: #111111; }"},
    )
    assert res.status_code in (200, 204)
    baks = [
      e for e in os.listdir(shared)
      if e.startswith("theme.css.bak-")
    ]
    assert baks == []
    # Second write: prior exists, snapshot must fire.
    res = client.put(
      "/api/storage/shared/theme.css",
      headers=auth,
      json={"content": ":root { --bg: #222222; }"},
    )
    assert res.status_code in (200, 204)
    baks = [
      e for e in os.listdir(shared)
      if e.startswith("theme.css.bak-")
    ]
    assert len(baks) == 1
    # Backup contains the FIRST write's contents — the snapshot
    # fires before the new write lands.
    bak_path = os.path.join(shared, baks[0])
    with open(bak_path) as f:
      assert f.read() == ":root { --bg: #111111; }"
  finally:
    for entry in list(os.listdir(shared)):
      if entry.startswith("theme.css"):
        os.remove(os.path.join(shared, entry))


