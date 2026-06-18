"""GET / serializes the effective theme into the __mobius-theme__ JSON slot.

This is the server half of the theme-as-data handoff that replaced
`inject_theme_into_html`. The server no longer injects a <style> block; it
fills `<script type="application/json" id="__mobius-theme__">` with the
effective `{css, bg, mode}` and the client paints it.

The load-bearing security property: the slot's text is owner-controlled CSS
embedded inside a <script> element, so an embedded `</script>` (or any `</`)
would break the element open. main.py escapes `</` -> `<\\/` (+ U+2028/2029)
so the slot can't be a script-injection vector even with hostile theme CSS.
"""

import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-exactly-32-chars!!")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mobius_test/test.db")
os.environ.setdefault("DATA_DIR", "/tmp/mobius_test")
os.environ.setdefault("FRONTEND_ORIGIN", "http://localhost:5173")

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

import app.main as main
from app.config import get_settings


SLOT_EMPTY = '<script type="application/json" id="__mobius-theme__"></script>'


def _reset_theme(data_dir: str):
  """Clear any theme.css/theme-mode + the effective-theme memo so each test
  starts from a known state."""
  shared = Path(data_dir) / "shared"
  shared.mkdir(parents=True, exist_ok=True)
  for name in ("theme.css", "theme-mode"):
    p = shared / name
    if p.exists():
      p.unlink()
  from app import theme as theme_mod
  theme_mod._EFFECTIVE_THEME_MEMO.clear()


@pytest.fixture
def static_index():
  # conftest.py creates backend/static/index.html (with the empty slot)
  # before app.main is imported, so the SPA fallback route GET / hits is
  # registered. Assert it's present, then serve it.
  idx = main._static_dir / "index.html"
  assert idx.is_file(), (
    "static/index.html missing — conftest should have stubbed it before "
    "app import; the SPA fallback route won't be registered without it"
  )
  assert SLOT_EMPTY in idx.read_text(encoding="utf-8")
  yield idx


def _extract_slot(html: str) -> str:
  """Return the inner text of the __mobius-theme__ script element."""
  open_tag = '<script type="application/json" id="__mobius-theme__">'
  i = html.index(open_tag) + len(open_tag)
  j = html.index("</script>", i)
  return html[i:j]


def test_index_slot_filled_with_valid_json(client, static_index):
  """GET / fills the slot with a parseable {css, bg, mode} bundle."""
  data_dir = get_settings().data_dir
  _reset_theme(data_dir)
  res = client.get("/")
  assert res.status_code == 200
  html = res.text
  # The slot must no longer be empty.
  assert SLOT_EMPTY not in html
  slot = _extract_slot(html)
  bundle = json.loads(slot)
  assert set(bundle) == {"css", "bg", "mode"}
  assert bundle["bg"] == "#0d0d0d"
  assert bundle["mode"] == "dark"
  assert ":root" in bundle["css"]


def test_index_slot_light_theme(client, static_index):
  """A light theme.css + theme-mode → the slot carries the light bundle."""
  data_dir = get_settings().data_dir
  _reset_theme(data_dir)
  shared = Path(data_dir) / "shared"
  (shared / "theme.css").write_text(":root { --bg: #f0eeeb; --text: #1c1b1a; }")
  (shared / "theme-mode").write_text(json.dumps("light"))
  from app import theme as theme_mod
  theme_mod._EFFECTIVE_THEME_MEMO.clear()
  res = client.get("/")
  assert res.status_code == 200
  bundle = json.loads(_extract_slot(res.text))
  assert bundle["bg"] == "#f0eeeb"
  assert bundle["mode"] == "light"
  assert "--bg: #f0eeeb" in bundle["css"]


def test_index_slot_escapes_script_breakout(client, static_index):
  """Malicious theme CSS containing `</script><script>...` and `</style>`
  cannot break out of the slot's <script type=application/json> wrapper.

  This is the slot-XSS regression guard — the theme-as-data analogue of the
  old `</style>` breakout test for inject_theme_into_html."""
  data_dir = get_settings().data_dir
  _reset_theme(data_dir)
  shared = Path(data_dir) / "shared"
  malicious = (
    ":root { --bg: #0d0d0d; }\n"
    "/* </script><script>window.__pwned=1</script> */\n"
    "/* </style> */\n"
  )
  (shared / "theme.css").write_text(malicious)
  from app import theme as theme_mod
  theme_mod._EFFECTIVE_THEME_MEMO.clear()

  res = client.get("/")
  assert res.status_code == 200
  html = res.text
  slot = _extract_slot(html)

  # 1. No literal `</script>` survived inside the slot text — the escape
  #    turned every `</` into `<\/`, so the wrapper closes only at the real
  #    end (our _extract_slot found the FIRST </script>, which must be the
  #    wrapper's own close, AFTER the full payload).
  assert "</script>" not in slot, f"literal </script> leaked into slot:\n{slot}"
  # The escaped form must be present (the payload still carries the text).
  assert "<\\/script>" in slot

  # 2. The slot still round-trips to the original CSS (escape is reversible:
  #    JSON treats \/ as /, so JSON.parse recovers the literal </script>).
  bundle = json.loads(slot.replace("<\\/", "</"))
  assert "</script>" in bundle["css"]
  assert "window.__pwned=1" in bundle["css"]

  # 3. Parse the served HEAD as HTML: the only <script> the parser sees must
  #    be the JSON slot itself — NO injected <script> from the payload.
  head = html.split("</head>")[0] + "</head>"

  class ScriptCounter(HTMLParser):
    def __init__(self):
      super().__init__()
      self.scripts = 0
      self.script_types = []

    def handle_starttag(self, tag, attrs):
      if tag == "script":
        self.scripts += 1
        self.script_types.append(dict(attrs).get("type"))

  parser = ScriptCounter()
  parser.feed(head)
  # Exactly one <script> in the head — the application/json slot. A breakout
  # would surface a second <script> (the injected one) with no/other type.
  assert parser.scripts == 1, (
    f"expected exactly one <script> in head (the slot), got "
    f"{parser.scripts}: types={parser.script_types}"
  )
  assert parser.script_types == ["application/json"]
  # And the pwned marker must NOT appear as executable script text outside the
  # JSON slot (it's inert inside the JSON string).
  assert "window.__pwned=1</script>" not in html
