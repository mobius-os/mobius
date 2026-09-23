#!/usr/bin/env python3
"""Request transient input and consume it without exposing submitted values."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
from urllib.parse import quote
import urllib.request


BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
  sys.path.insert(0, str(BACKEND_ROOT))


def _post(
  url: str, payload: dict, token: str | None = None, *,
  timeout: float | None = 35,
) -> tuple[int, dict]:
  headers = {"Content-Type": "application/json"}
  if token:
    headers["Authorization"] = f"Bearer {token}"
  request = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers=headers,
    method="POST",
  )
  try:
    with urllib.request.urlopen(request, timeout=timeout) as response:
      return response.status, json.loads(response.read() or b"{}")
  except urllib.error.HTTPError as exc:
    try:
      body = json.loads(exc.read() or b"{}")
    except Exception:
      body = {}
    return exc.code, body


def _safe_error(body: dict, fallback: str) -> str:
  detail = body.get("detail")
  return detail if isinstance(detail, str) else fallback


def _field(value: str) -> dict:
  parts = value.split(":", 2)
  if len(parts) != 3:
    raise argparse.ArgumentTypeError("field must be name:type:label")
  name, input_type, label = parts
  if input_type not in {"text", "password"}:
    raise argparse.ArgumentTypeError("field type must be text or password")
  return {"name": name, "type": input_type, "label": label}


def _named_source(value: str) -> tuple[str, str]:
  name, separator, source = value.partition("=")
  if not separator or not name or not source:
    raise argparse.ArgumentTypeError("Expected FIELD=SOURCE")
  return name, source


def _submit_saved(args) -> int:
  """Submit an existing authorized local value without model-visible bytes."""
  base = os.environ.get("API_BASE_URL", "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN", "")
  if not base or not token:
    print("Secure input submission is unavailable in this agent turn.")
    return 2
  values: dict[str, str] = {}
  try:
    for name, source in args.field_file:
      if name in values:
        raise ValueError("Duplicate field name")
      value = Path(source).read_text(encoding="utf-8")
      # Conventional secret files commonly end in one line terminator. Remove
      # exactly that terminator without broadly stripping meaningful spaces or
      # additional newlines from the credential.
      if value.endswith("\r\n"):
        value = value[:-2]
      elif value.endswith("\n"):
        value = value[:-1]
      values[name] = value
    for name, source in args.field_env:
      if name in values or source not in os.environ:
        raise ValueError("Duplicate field or missing source")
      values[name] = os.environ[source]
    status, result = _post(
      f"{base}/api/secure-inputs/{quote(args.chat_id, safe='')}/"
      f"{quote(args.request_id, safe='')}/submit",
      {"fields": values}, token,
    )
    if (status != 200 or not isinstance(result, dict)
        or result.get("status") != "consuming"):
      raise ValueError("Secure input not accepted")
    print("Secure input accepted; check the card for its final outcome.")
    return 0
  except (OSError, ValueError):
    print("Secure input submission failed; check the card status.")
    return 1
  finally:
    values.clear()


def _request_saved(spec: dict, command: list[str], action: str) -> dict:
  """Commit a sealed request, without retaining a waiting helper or values."""
  base = os.environ.get("API_BASE_URL", "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN", "")
  chat_id = os.environ.get("CHAT_ID", "")
  if not base or not token or not chat_id:
    raise RuntimeError("Secure input is unavailable: chat environment is incomplete.")
  if not command:
    raise RuntimeError("A sealed consumer command is required.")
  status, receipt = _post(
    f"{base}/api/secure-inputs/{chat_id}/saved",
    {**spec, "command": command, "cwd": os.getcwd(), "action": action}, token,
  )
  if status >= 300:
    raise RuntimeError("Could not save secure input; no values were requested. Retry the identical request.")
  if (receipt.get("state") not in {"waiting_for_owner", "answered"}
      or not receipt.get("question_id") or not receipt.get("next_action")):
    raise RuntimeError("Secure input save was not confirmed. Retry the identical request.")
  return receipt


def _request_and_consume(spec: dict) -> tuple[str, str, dict[str, str]]:
  base = os.environ.get("API_BASE_URL", "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN", "")
  chat_id = os.environ.get("CHAT_ID", "")
  if not base or not token or not chat_id:
    raise RuntimeError("Secure input is unavailable: chat environment is incomplete.")

  status, created = _post(
    f"{base}/api/secure-inputs/{chat_id}", spec, token,
  )
  if status >= 300:
    raise RuntimeError(_safe_error(created, "Could not open secure input."))
  request_id = created.get("request_id")
  capability = created.get("capability")
  if not request_id or not capability:
    raise RuntimeError("Could not open secure input: invalid server response.")

  try:
    # The local server parks this call until the owner submits or cancels.
    # Keep ordinary transport deadlines for machine work, never human input.
    status, state = _post(
      f"{base}/api/secure-inputs/{request_id}/wait",
      {"capability": capability},
      timeout=None,
    )
    if status >= 300:
      raise RuntimeError("Secure input became unavailable.")
    if state.get("status") != "filled":
      result = state.get("result") or {}
      raise RuntimeError(result.get("message") or "Secure input closed.")
    status, consumed = _post(
      f"{base}/api/secure-inputs/{request_id}/consume",
      {"capability": capability},
    )
    if status >= 300 or not isinstance(consumed.get("fields"), dict):
      raise RuntimeError("Secure input could not be consumed.")
    values = {
      key: value for key, value in consumed["fields"].items()
      if isinstance(key, str) and isinstance(value, str)
    }
    consumed["fields"].clear()
    return request_id, capability, values
  except BaseException:
    _post(
      f"{base}/api/secure-inputs/{request_id}/cancel",
      {"capability": capability},
    )
    raise


def _settle(request_id: str, capability: str, *, ok: bool, message: str) -> None:
  base = os.environ.get("API_BASE_URL", "").rstrip("/")
  _post(
    f"{base}/api/secure-inputs/{request_id}/settle",
    {"capability": capability, "ok": ok, "message": message[:240]},
  )


def _owner_credentials_consumer() -> list[str]:
  return [
    sys.executable,
    str(Path(__file__).with_name("update-owner-credentials.py")),
  ]


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Request secure input without adding values to model context.",
  )
  sub = parser.add_subparsers(dest="action", required=True)

  owner = sub.add_parser("owner-credentials")
  owner.set_defaults(
    mode="sealed",
    title="Update sign-in",
    description=(
      "Values go directly to a local credential updater and are not sent to "
      "the AI provider."
    ),
    fields=[
      {"name": "current_password", "type": "password", "label": "Current password", "autocomplete": "current-password"},
      {"name": "new_username", "type": "text", "label": "New username", "autocomplete": "username"},
      {"name": "new_password", "type": "password", "label": "New password", "autocomplete": "new-password"},
      {"name": "confirm_password", "type": "password", "label": "Confirm new password", "autocomplete": "new-password"},
    ],
    command=_owner_credentials_consumer(),
  )

  run = sub.add_parser("run")
  run.add_argument("--title", required=True)
  run.add_argument("--description", default="")
  run.add_argument("--field", action="append", required=True, type=_field)
  run.add_argument("command", nargs=argparse.REMAINDER)

  reveal = sub.add_parser("reveal")
  reveal.add_argument("--title", required=True)
  reveal.add_argument("--description", default="")
  reveal.add_argument("--field", action="append", required=True, type=_field)

  submit = sub.add_parser("submit-saved")
  submit.add_argument("--chat-id", required=True)
  submit.add_argument("--request-id", required=True)
  submit.add_argument("--field-file", action="append", type=_named_source, default=[])
  submit.add_argument("--field-env", action="append", type=_named_source, default=[])

  args = parser.parse_args()
  if args.action == "submit-saved":
    if not args.field_file and not args.field_env:
      parser.error("submit-saved needs --field-file or --field-env")
    return _submit_saved(args)
  if args.action == "run":
    args.mode = "sealed"
    args.fields = args.field
    args.command = args.command[1:] if args.command[:1] == ["--"] else args.command
  elif args.action == "reveal":
    args.mode = "reveal"
    args.fields = args.field
    args.command = None

  spec = {
    "mode": args.mode,
    "title": args.title,
    "description": args.description,
    "fields": args.fields,
  }
  if args.action != "reveal":
    try:
      print(json.dumps(_request_saved(spec, args.command, args.action)))
      return 0
    except Exception:
      print("Secure input save was not confirmed. Retry the identical request; do not continue as if values were provided.")
      return 1

  request_id = capability = None
  values: dict[str, str] = {}
  try:
    request_id, capability, values = _request_and_consume(spec)
    from app.secure_inputs import build_reveal_envelope
    print(build_reveal_envelope(json.dumps(values, ensure_ascii=False)))
    rc = 0
    ok = True
    message = "Secure values were revealed to the model for this turn only."
    _settle(request_id, capability, ok=ok, message=message)
    return rc
  except (KeyboardInterrupt, EOFError):
    if request_id and capability:
      _settle(
        request_id,
        capability,
        ok=False,
        message="Secure input was cancelled; submitted values were discarded.",
      )
    print("Secure input cancelled.")
    return 130
  except Exception as exc:
    if request_id and capability:
      _settle(
        request_id,
        capability,
        ok=False,
        message="The sealed consumer failed; submitted values were discarded.",
      )
      print("Secure input failed; submitted values were discarded.")
    else:
      print(str(exc))
    return 1
  finally:
    values.clear()


if __name__ == "__main__":
  sys.exit(main())
