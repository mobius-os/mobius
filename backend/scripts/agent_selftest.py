#!/usr/bin/env python3
"""Bounded synthetic-turn diagnostic: prove the REAL chat pipeline end to end.

Basic HTTP health cannot see the failure the 2026-09-01 incident exposed: the
web/API surface was fine while every chat turn died. This drives a short-lived,
self-cleaning throwaway chat turn through the live provider pipeline and scores
it with ``deploy_support.classify_synthetic_turn`` — which REJECTS an
error/resumable/empty reply (the exact state a full-/data admission deferral
emits, and the false-green the old deploy canary scored as passing).

It proves, for real: message persistence, runner admission, provider invocation,
stream/terminal completion, and NON-EMPTY assistant output.

Operator/deploy contract: repeatable (each run owns one throwaway chat), low-cost (a
trivial no-tool prompt), timeout-bounded, and it cleans up ONLY the chat it
created. It never prints the service token. Exit codes: 0 passed, 2 failed
(pipeline broken), 3 unavailable (no token / no provider / setup error) so a
deploy can distinguish "provider not connected" (warn) from "pipeline broken"
(fail + roll back).

Usage:
  agent_selftest.py [--expect-provider codex|claude|mobius]
                    [--expect-model <id>] [--timeout 90]
                    [--prompt 'Reply with exactly: OK'] [--keep]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

_DEPLOY_SUPPORT = Path(__file__).resolve().parents[2] / "scripts" / "deploy_support.py"


def _load_classifier():
  spec = importlib.util.spec_from_file_location("deploy_support", _DEPLOY_SUPPORT)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module.classify_synthetic_turn


def _service_token() -> str | None:
  # Prefer the explicit in-container owner service token; never echo it.
  for path in (
    os.environ.get("MOBIUS_SERVICE_TOKEN_FILE"),
    "/data/service-token.txt",
  ):
    if not path:
      continue
    try:
      token = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
      continue
    if token:
      return token
  token = os.environ.get("AGENT_TOKEN")
  return token.strip() if token else None


def _request(
  base: str,
  method: str,
  path: str,
  token: str,
  body: dict | None = None,
  *,
  timeout: float = 30,
):
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(base + path, data=data, method=method)
  req.add_header("Authorization", f"Bearer {token}")
  if data is not None:
    req.add_header("Content-Type", "application/json")
  with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator URL)
    raw = resp.read().decode("utf-8")
  return json.loads(raw) if raw else {}


def run_selftest(
  *,
  base: str,
  token: str,
  expected_provider: str | None,
  expected_model: str | None,
  prompt: str,
  timeout: float,
  keep: bool,
  clock: Callable[[], float] = time.monotonic,
  sleep: Callable[[float], None] = time.sleep,
) -> dict:
  classify = _load_classifier()
  started = clock()
  deadline = started + timeout
  chat_id = None
  outcome = None

  def request(method: str, path: str, body: dict | None = None):
    remaining = deadline - clock()
    if remaining <= 0:
      raise TimeoutError
    return _request(
      base, method, path, token, body, timeout=min(30.0, remaining),
    )

  try:
    # The owner chat-create contract snapshots the owner's current complete
    # picker choice. Do not send provider/model fields here: ChatCreate does
    # not own them, and switching the new chat through the ordinary settings
    # route would also mutate the owner's saved picker defaults. Expectations
    # are checked below without changing any persistent owner setting.
    create_body: dict = {"title": "Deploy self-test", "messages": []}
    chat = request("POST", "/api/chats", create_body)
    chat_id = chat.get("id")
    if not chat_id:
      outcome = {"result": "failed", "reason": "chat create returned no id"}
      return outcome

    detail = chat.get("detail") if isinstance(chat.get("detail"), dict) else chat
    provider = detail.get("provider")
    effective = detail.get("effective_agent_settings")
    model = effective.get("model") if isinstance(effective, dict) else None
    if expected_provider and provider != expected_provider:
      outcome = {
        "result": "unavailable",
        "reason": "active provider does not match expectation",
        "provider": provider,
        "model": model,
      }
      return outcome
    if expected_model and model != expected_model:
      outcome = {
        "result": "unavailable",
        "reason": "active model does not match expectation",
        "provider": provider,
        "model": model,
      }
      return outcome

    request(
      "POST",
      f"/api/chats/{chat_id}/messages",
      {"content": prompt, "hidden": True},
    )

    last = {}
    while clock() < deadline:
      last = request("GET", f"/api/chats/{chat_id}?limit=10")
      verdict = classify(last)
      if verdict != "pending":
        outcome = {
          "result": "passed" if verdict == "passed" else "failed",
          "latency_s": round(clock() - started, 2),
          "provider": provider,
          "model": model,
        }
        return outcome
      sleep(min(1.0, max(0.0, deadline - clock())))
    outcome = {
      "result": "failed",
      "reason": "timeout",
      "latency_s": round(clock() - started, 2),
      "provider": provider,
      "model": model,
    }
    return outcome
  except urllib.error.HTTPError as exc:
    # Auth/setup conflicts are unavailable operator prerequisites. Rate limits,
    # timeouts, and server failures mean the real turn pipeline did not work.
    unavailable = exc.code in (401, 403, 409, 422)
    outcome = {
      "result": "unavailable" if unavailable else "failed",
      "reason": f"http {exc.code}",
    }
    return outcome
  except (TimeoutError, urllib.error.URLError, OSError, TypeError, ValueError) as exc:
    outcome = {"result": "failed", "reason": type(exc).__name__}
    return outcome
  finally:
    if chat_id and keep and isinstance(outcome, dict):
      outcome["chat_id"] = chat_id
    elif chat_id:
      try:
        _request(
          base,
          "DELETE",
          f"/api/chats/{chat_id}",
          token,
          timeout=min(5.0, max(0.1, timeout)),
        )
      except Exception:
        # Cleanup failure does not turn a proven provider round-trip red, but it
        # must be visible so the operator can reclaim the exact throwaway chat.
        if isinstance(outcome, dict):
          outcome["cleanup"] = "failed"
          outcome["cleanup_chat_id"] = chat_id


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
    "--expect-provider", choices=("claude", "codex", "mobius"), default=None,
  )
  parser.add_argument("--expect-model", default=None)
  parser.add_argument("--prompt", default="Reply with exactly: OK")
  parser.add_argument("--timeout", type=float, default=90.0)
  parser.add_argument("--keep", action="store_true")
  parser.add_argument("--base", default=os.environ.get("API_BASE_URL", "http://localhost:8000"))
  args = parser.parse_args(argv)

  token = _service_token()
  if not token:
    print(json.dumps({"result": "unavailable", "reason": "no service token"}))
    return 3

  outcome = run_selftest(
    base=args.base.rstrip("/"), token=token,
    expected_provider=args.expect_provider,
    expected_model=args.expect_model,
    prompt=args.prompt, timeout=args.timeout, keep=args.keep,
  )
  print(json.dumps(outcome, sort_keys=True))
  return {"passed": 0, "failed": 2, "unavailable": 3}.get(outcome["result"], 3)


if __name__ == "__main__":
  raise SystemExit(main())
