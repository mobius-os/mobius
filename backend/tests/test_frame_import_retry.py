"""The frame's module-import retry must stay present, bounded, gated, and
cache-busted.

loadModule in app-frame.html retries a transiently-failed dynamic import
once before painting the "Failed to load app" panel — mobile networks
routinely drop the first fetch after device wake, and without the retry a
single blip is a permanent error panel for EVERY app. The retry must stay
narrow: only network-class failures (TypeError), only pre-mount
(`window.__frameMounted` falsy), only ONE extra attempt, and only AFTER
the auth probe so an expired token still routes to the parent's
token-refresh re-init instead of a blind re-import with the same dead
token. It must ALSO cache-bust the retry URL: Chromium caches a failed
dynamic import per exact URL, so re-importing the SAME url after a blip
resolves to the cached failure with no new fetch — the retry would be a
no-op. The retry imports `moduleUrl(1)` (a fresh `_=` param) so the
browser actually re-fetches. These tests regex-lock each of those
properties so a refactor can't silently drop the retry, widen it into a
loop, or regress it back to a no-op same-URL re-import.
"""

import re
from pathlib import Path

import pytest

from app.config import get_settings


def _find_app_frame() -> Path | None:
  """Resolve app-frame.html the same way the frame route does, plus the
  repo-relative path so the local (non-Docker) test run finds it too."""
  candidates = [
    Path(get_settings().data_dir) / "shell" / "public" / "app-frame.html",
    Path(__file__).resolve().parents[2] / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
    Path("/app/static/app-frame.html"),
  ]
  return next((p for p in candidates if p.exists()), None)


def _frame_html() -> str:
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  return frame.read_text()


def test_retry_gates_on_typeerror_and_premount():
  html = _frame_html()
  guard = re.search(
    r"instanceof TypeError\)\s*\|\|\s*window\.__frameMounted", html
  )
  assert guard, (
    "the import retry no longer gates on (TypeError + pre-mount) — either "
    "deterministic failures (bad module syntax) would be retried "
    "pointlessly, or a future post-mount loadModule call could re-import "
    "and double-render a live app."
  )


def test_retry_is_delayed_and_single():
  html = _frame_html()
  delayed = re.search(
    r"setTimeout\(\s*r\s*,\s*IMPORT_RETRY_DELAY_MS\s*\)[\s\S]{0,400}?"
    r"await import\(moduleUrl\(1\)\)",
    html,
  )
  assert delayed, (
    "no delayed second `await import(moduleUrl(1))` after "
    "IMPORT_RETRY_DELAY_MS — the transient-import retry was dropped (one "
    "network blip after device wake becomes a permanent 'Failed to load "
    "app' panel)."
  )
  # Structurally bounded: exactly one retry import. A loop or a third
  # attempt would show up as more retry sites.
  assert len(re.findall(r"await import\(moduleUrl\(1\)\)", html)) == 1, (
    "expected exactly one retry `await import(moduleUrl(1))` site (one "
    "bounded retry) — a loop or extra attempt changes the retry contract."
  )
  # The initial attempt imports the un-busted base url; the retry imports a
  # DISTINCT, cache-busted url. Same-url re-imports are the no-op this fix
  # removed (Chromium caches the failed import per exact URL).
  assert re.search(r"const url = moduleUrl\(0\)", html), (
    "initial import no longer uses moduleUrl(0) (the un-busted base url)."
  )


def test_retry_url_is_cache_busted():
  html = _frame_html()
  # moduleUrl(retry) appends a fresh `_=<retry>` only when retry is truthy,
  # so the cold-start url (retry 0) is unchanged and the retry url differs.
  builder = re.search(
    r"const moduleUrl = \(retry\) =>[\s\S]{0,1400}?"
    r"return retry \? `\$\{base\}&_=\$\{retry\}` : base",
    html,
  )
  assert builder, (
    "moduleUrl no longer cache-busts the retry with a fresh `_=` param — "
    "Chromium caches the failed dynamic import per exact URL, so a same-url "
    "retry resolves to the cached failure with no new fetch (a no-op). The "
    "`_` param is stripped by the SW offline cache key and ignored by the "
    "/module route, so it busts only the browser's import cache."
  )


def test_retry_runs_after_auth_probe():
  html = _frame_html()
  token_expired = html.find("moebius:token-expired")
  retry_delay = html.find("IMPORT_RETRY_DELAY_MS = ")
  assert token_expired != -1 and retry_delay != -1, (
    "auth-probe (token-expired) path or retry-delay constant missing from "
    "loadModule."
  )
  retry_use = html.find("IMPORT_RETRY_DELAY_MS", retry_delay + 1)
  assert token_expired < retry_use, (
    "the retry runs before the auth probe — an expired token would be "
    "re-imported blind instead of routed to the parent's token-refresh "
    "re-init."
  )
