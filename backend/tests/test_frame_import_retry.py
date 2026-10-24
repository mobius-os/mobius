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
from tests.frame_entrypoint_probe import run_frame_entrypoint


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
  first_token_expired = html.find(
    "renegotiateExpiredModuleToken(importErr)", retry_delay,
  )
  retry_use = html.find("IMPORT_RETRY_DELAY_MS", retry_delay + 1)
  assert retry_delay != -1 and first_token_expired != -1 and retry_use != -1
  assert first_token_expired < retry_use
  assert html.count("renegotiateExpiredModuleToken(retryErr)") == 1


_NETWORK_FAILURE = {"code": "network", "message": "parent fetch failed"}
_BLOB_FAILURE = {
  "name": "TypeError",
  "message": "Failed to fetch dynamically imported module: blob:https://frame.test/id",
}


@pytest.mark.parametrize("failure", [
  _NETWORK_FAILURE,
  _BLOB_FAILURE,
  {"name": "TypeError", "message": "error loading dynamically imported module: blob:null/id"},
], ids=["parent-network", "chromium-blob", "firefox-blob"])
def test_loader_retries_transport_once_only_after_the_delay(failure):
  observed = run_frame_entrypoint(_frame_html(), failures=[failure])
  waiting = observed["beforeDelay"]
  assert waiting["imports"] == [0]
  assert len(waiting["delays"]) == 1
  assert waiting["delays"][0] > 0
  assert waiting["renderCount"] == 0
  assert not waiting["mounted"] and not waiting["panel"]
  assert waiting["messages"] == []

  loaded = observed["beforeCommit"]
  assert loaded["imports"] == [0, 1]
  assert loaded["delays"] == waiting["delays"]
  assert loaded["renderCount"] == 1
  assert not loaded["mounted"]
  assert not loaded["configPresent"]
  assert observed["final"]["mounted"]
  assert observed["final"]["messages"] == ["moebius:frame-mounted"]


@pytest.mark.parametrize("failure", [
  {"name": "SyntaxError", "message": "Unexpected token"},
  {"name": "ReferenceError", "message": "missing app variable"},
  {"name": "TypeError", "message": "app property is not a function"},
  {"name": "TypeError", "message": "Failed to fetch dynamically imported module: https://app.test/a.js"},
  {"name": "Error", "message": _BLOB_FAILURE["message"]},
  {"code": "permission-denied", "message": "module access refused"},
], ids=["syntax", "reference", "app-typeerror", "non-blob-url", "non-typeerror", "refusal"])
def test_loader_does_not_replay_deterministic_evaluation_or_refusal(failure):
  observed = run_frame_entrypoint(_frame_html(), failures=[failure])
  failed = observed["final"]
  assert failed["imports"] == [0]
  assert failed["delays"] == []
  assert failed["renderCount"] == 0
  assert not failed["mounted"]
  assert failed["messages"] == ["moebius:frame-error"]
  assert failed["panel"] and failed["rootHidden"]
  assert failed["title"] == "Failed to load app"
  assert failure["message"] in failed["detail"]
  assert not failed["configPresent"]


@pytest.mark.parametrize("failure", [_NETWORK_FAILURE, _BLOB_FAILURE], ids=["network", "blob"])
def test_loader_does_not_retry_transport_after_a_real_mount(failure):
  observed = run_frame_entrypoint(_frame_html(), postmount=True, failures=[failure])
  assert observed["initial"]["mounted"]
  failed = observed["final"]
  assert failed["imports"] == [0]
  assert failed["delays"] == []
  assert failed["renderCount"] == 1  # only the successful setup render
  assert failed["messages"] == ["moebius:frame-mounted", "moebius:frame-error"]
  assert failed["panel"]
  assert failure["message"] in failed["detail"]


@pytest.mark.parametrize("second_failure", [
  _NETWORK_FAILURE, _BLOB_FAILURE, {"name": "SyntaxError", "message": "bad app syntax"},
], ids=["network-again", "blob-again", "deterministic-on-retry"])
def test_retry_failure_is_terminal_without_a_third_import(second_failure):
  observed = run_frame_entrypoint(
    _frame_html(), failures=[_NETWORK_FAILURE, second_failure],
  )
  failed = observed["final"]
  assert failed["imports"] == [0, 1]
  assert len(failed["delays"]) == 1
  assert failed["delays"][0] > 0
  assert failed["renderCount"] == 0
  assert not failed["mounted"]
  assert failed["messages"] == ["moebius:frame-error"]
  assert failed["panel"]
  assert second_failure["message"] in failed["detail"]
  assert not failed["configPresent"]


@pytest.mark.parametrize("on_retry", [False, True], ids=["first-import", "retry-import"])
def test_expired_token_renegotiation_precedes_even_blob_shaped_retry_errors(on_retry):
  expired = {**_BLOB_FAILURE, "code": "token-expired"}
  failures = [_NETWORK_FAILURE, expired] if on_retry else [expired]
  observed = run_frame_entrypoint(_frame_html(), failures=failures)
  waiting_for_token = observed["final"]
  assert waiting_for_token["imports"] == ([0, 1] if on_retry else [0])
  assert len(waiting_for_token["delays"]) == int(on_retry)
  assert all(delay > 0 for delay in waiting_for_token["delays"])
  assert not waiting_for_token["initialized"]
  assert not waiting_for_token["mounted"]
  assert waiting_for_token["renderCount"] == 0
  assert not waiting_for_token["panel"]
  assert waiting_for_token["messages"] == ["moebius:token-expired"]
  assert not waiting_for_token["configPresent"]
