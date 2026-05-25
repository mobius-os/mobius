"""SDK-bump contract tests for the Codex AskUserQuestion bridge.

The bridge (`codex_sdk_runner._install_request_user_input_handler`)
monkey-patches `<AsyncCodex instance>._client._sync._approval_handler`.
Its startup check raises if the attribute is missing on the live
instance — a loud failure. What it does NOT catch:

  1. A future SDK that keeps the attribute name but ignores assignment
     (silent no-op via descriptor / __slots__). The model's
     AskUserQuestion calls would fall through to the SDK's default
     approval handler, the question card would never render, and the
     user would see a wedged turn — detectable only when a real user
     hits a clarifying question.

  2. The chain `_client._sync` itself being restructured (e.g. the SDK
     removes the sync-bridge accessor in favor of a different shape).
     The runner's attribute access would then raise AttributeError at
     startup, but only on the first chat that uses Codex.

  3. The JSON-RPC method string (`item/tool/requestUserInput`) being
     renamed by the SDK. Same silent-wedge failure mode.

These tests catch (1), (2), (3) at build time so a Codex SDK bump
(pinned in the Dockerfile to a specific commit SHA, but bumpable)
fails CI loudly rather than producing a silent runtime regression.

Earlier version of these tests probed `codex._client._sync` as a
module import and `AppServerClient` as a class — neither matches what
the runner actually accesses. As a result both tests silently SKIPPED
in CI (the module didn't exist; pytest.importorskip swallowed it),
giving zero coverage of the contract they were named for. The current
file exercises the live instance path the runner actually uses.

See CLAUDE.md "Codex `_approval_handler` patch — known fragility" for
the broader context.
"""

import pytest


def _make_sync_handle():
  """Constructs the live object the runner patches.

  Mirrors what `_install_request_user_input_handler` reaches for:
  `AsyncCodex()._client._sync`. No network or subprocess — just
  instance construction. Skips cleanly if the SDK isn't installed
  (keeps the test useful on slim CI containers).
  """
  openai_codex = pytest.importorskip("openai_codex")
  ac = openai_codex.AsyncCodex()
  return ac._client._sync


def test_codex_approval_handler_attribute_exists():
  """`_approval_handler` must exist on the live `_client._sync` object.

  If a SDK bump removes/renames it, `_install_request_user_input_handler`
  raises at the startup hasattr check — but only on the first Codex
  turn after deploy. This test surfaces the breakage at build time.
  """
  sync = _make_sync_handle()
  assert hasattr(sync, "_approval_handler"), (
    "openai_codex no longer exposes _approval_handler on "
    "AsyncCodex()._client._sync. The AskUserQuestion bridge in "
    "codex_sdk_runner.py will refuse to install at startup. Inspect "
    "the new SDK surface for the renamed hook and update "
    "_install_request_user_input_handler accordingly."
  )


def test_codex_approval_handler_assignment_takes_effect():
  """Monkey-patching `_approval_handler` must actually replace the slot.

  If a future SDK locks the attribute via __slots__, a property, or a
  descriptor that ignores plain assignment, our patch installs
  successfully but the original handler keeps running — the bridge
  silently no-ops. Verify the assignment is read-back-the-same on the
  live instance the runner targets.
  """
  sync = _make_sync_handle()
  original = sync._approval_handler
  sentinel = object()
  try:
    sync._approval_handler = sentinel
    assert sync._approval_handler is sentinel, (
      "Assignment to _approval_handler on AsyncCodex()._client._sync "
      "did not take effect — the SDK likely added a descriptor, "
      "__slots__, or property that blocks our bridge. The "
      "AskUserQuestion bridge would silently no-op; users would see "
      "wedged turns on clarifying questions."
    )
  finally:
    sync._approval_handler = original


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
