"""The opaque frame's parent-broker retry stays bounded and intentional.

The compiled blob is dependency-complete, so app evaluation failures must not
be replayed. A typed parent-fetch failure or the browser's specific failure to
consume a dynamic ``blob:`` import gets one delayed, pre-mount retry. Retry
``1`` is forwarded to the parent so its versioned module request can bypass a
cached transient response without changing the service-worker cache identity.
"""

import re
from pathlib import Path

import pytest

from app.config import get_settings


def _find_app_frame() -> Path | None:
  candidates = [
    Path(get_settings().data_dir) / "shell" / "public" / "app-frame.html",
    Path(__file__).resolve().parents[2] / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
    Path("/app/static/app-frame.html"),
  ]
  return next((path for path in candidates if path.exists()), None)


def _frame_html() -> str:
  frame = _find_app_frame()
  if frame is None:
    pytest.skip("app-frame.html not resolvable in this environment")
  return frame.read_text()


def test_retry_gates_on_transport_failure_and_premount():
  html = _frame_html()
  assert re.search(
    r"retryable = Boolean\(importErr && importErr\.code === 'network'\)"
    r"\s*\|\| isBlobModuleLoadFailure\(importErr\);"
    r"[\s\S]{0,180}?if \(!retryable \|\| window\.__frameMounted\)",
    html,
  )


def test_blob_retry_classifier_is_specific_to_dynamic_blob_imports():
  html = _frame_html()
  assert "error.name !== 'TypeError'" in html
  assert re.search(
    r"\(\?:failed to fetch\|error loading\) dynamically imported "
    r"module:\\s\*blob:",
    html,
  )
  assert "importErr instanceof TypeError" not in html


def test_retry_is_delayed_and_single():
  html = _frame_html()
  assert re.search(
    r"setTimeout\(\s*r\s*,\s*IMPORT_RETRY_DELAY_MS\s*\)[\s\S]{0,250}?"
    r"await importBrokeredModule\(1\)",
    html,
  )
  assert len(re.findall(r"await importBrokeredModule\(1\)", html)) == 1
  assert len(re.findall(r"await importBrokeredModule\(0\)", html)) == 1


def test_retry_marker_reaches_the_parent_broker():
  html = _frame_html()
  assert re.search(
    r"type: 'moebius:module-request', requestId,[\s\S]{0,100}?"
    r"retry: retry === 1 \? 1 : 0",
    html,
  )


def test_retry_runs_after_token_renegotiation_branch():
  html = _frame_html()
  retry_delay = html.find("IMPORT_RETRY_DELAY_MS = ")
  first_token_expired = html.find("importErr.code === 'token-expired'", retry_delay)
  retry_use = html.find("IMPORT_RETRY_DELAY_MS", retry_delay + 1)
  assert retry_delay != -1 and first_token_expired != -1 and retry_use != -1
  assert first_token_expired < retry_use
