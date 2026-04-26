import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-exactly-32-chars!!")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mobius_test/test.db")
os.environ.setdefault("DATA_DIR", "/tmp/mobius_test")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:5173")

from app.theme import inject_theme_into_html, extract_imports


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
  # DEFAULT_THEME has --bg: #0d0f14
  assert body["bg"] == "#0d0f14"


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
