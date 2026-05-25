"""SDK-bump contract tests for the Codex AskUserQuestion bridge.

The bridge (`codex_sdk_runner._install_request_user_input_handler`)
monkey-patches `AppServerClient._approval_handler`. Its startup check
raises if the attribute is missing — a loud failure. What it does NOT
catch:

  1. A future SDK that keeps the attribute name but ignores assignment
     (silent no-op). The model's AskUserQuestion calls would fall
     through to the SDK's default approval handler, the question card
     would never render, and the user would see a wedged turn —
     detectable only when a real user hits a clarifying question.

  2. The JSON-RPC method string (`item/tool/requestUserInput`) being
     renamed by the SDK. Same silent-wedge failure mode.

These tests catch (1) at build time so a Codex SDK bump (pinned in
the Dockerfile to a specific commit SHA, but bumpable) fails CI loudly
rather than producing a silent runtime regression.

See CLAUDE.md "Codex `_approval_handler` patch — known fragility" for
the broader context.
"""

import pytest


def test_codex_approval_handler_attribute_exists():
  """`AppServerClient._approval_handler` must exist on the SDK class.

  If a SDK bump removes/renames it, `_install_request_user_input_handler`
  fails at server startup — but only on the first Codex turn. This test
  surfaces the breakage at build time.
  """
  AppServerClient = pytest.importorskip(
    "codex._client._sync"
  ).AppServerClient
  assert hasattr(AppServerClient, "_approval_handler"), (
    "Codex SDK no longer exposes _approval_handler on AppServerClient. "
    "The AskUserQuestion bridge in codex_sdk_runner.py will not install. "
    "Inspect the SDK's _client._sync module for the renamed attribute "
    "and update _install_request_user_input_handler accordingly."
  )


def test_codex_approval_handler_assignment_takes_effect():
  """Monkey-patching `_approval_handler` must actually replace the method.

  If a future SDK locks the attribute via __slots__ or a descriptor
  that ignores assignment, our patch installs successfully but the
  original handler keeps running — the bridge silently no-ops.
  """
  sync_mod = pytest.importorskip("codex._client._sync")
  AppServerClient = sync_mod.AppServerClient

  original = getattr(AppServerClient, "_approval_handler", None)
  sentinel = object()
  try:
    AppServerClient._approval_handler = sentinel
    assert AppServerClient._approval_handler is sentinel, (
      "Assignment to AppServerClient._approval_handler did not take "
      "effect — the SDK likely added a descriptor or __slots__ that "
      "blocks our bridge. The AskUserQuestion bridge would silently "
      "no-op; users would see wedged turns on clarifying questions."
    )
  finally:
    if original is None:
      delattr(AppServerClient, "_approval_handler")
    else:
      AppServerClient._approval_handler = original


def test_request_user_input_method_string_unchanged():
  """The JSON-RPC method string our handler keys on must still match.

  Today: `item/tool/requestUserInput`. If the SDK renames the RPC
  method (e.g. to `tool/requestUserInput`), `_approval_handler` is
  invoked but our `_REQUEST_USER_INPUT_METHOD` mismatch sends the
  request to the SDK's default-accept path — silent failure.
  """
  from app.codex_sdk_runner import _REQUEST_USER_INPUT_METHOD
  assert _REQUEST_USER_INPUT_METHOD == "item/tool/requestUserInput", (
    "_REQUEST_USER_INPUT_METHOD has changed from the expected SDK "
    "wire string. Verify codex/_client docs and update the constant "
    "if the SDK genuinely renamed the method."
  )
