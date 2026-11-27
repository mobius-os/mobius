#!/usr/bin/env python3
"""Request transient input and consume it without exposing submitted values."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
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


class SecureInputRefused(RuntimeError):
  """The server refused the request; the message is its static reason."""


def _record_refusal(body: dict) -> str:
  """Name the server's reason; the request route returns static texts only."""
  detail = _safe_error(body, "The server refused the request.")
  return f"Could not record secure input; no values were requested. {detail}"


def _key_from_title(title: str) -> str:
  """A stable request key for a title, so repeating the command revises the
  same request instead of adding another."""
  slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:72].strip("-")
  return f"secret-{slug}" if slug else "secret"


def _record_secret_request(key: str, request: dict) -> dict:
  """Record a sealed request on this chat's owner-input list.

  The same request as `owner_input` with kind secret: it does not end the
  turn, and its card is shown when the turn ends. Values never pass here.
  """
  base = os.environ.get("API_BASE_URL", "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN", "")
  chat_id = os.environ.get("CHAT_ID", "")
  if not base or not token or not chat_id:
    raise RuntimeError("Secure input is unavailable: chat environment is incomplete.")
  status, result = _post(
    f"{base}/api/chats/{quote(chat_id, safe='')}/input-requests",
    {"op": "add", "kind": "secret", "key": key, **request}, token,
  )
  if status >= 300:
    raise SecureInputRefused(
      _record_refusal(result) + (" Retry the identical request." if status >= 500 else "")
    )
  if not isinstance(result, dict) or not isinstance(result.get("open"), list):
    raise RuntimeError("Secure input request was not confirmed. Retry the identical request.")
  return result


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


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Request secure input without adding values to model context.",
  )
  sub = parser.add_subparsers(dest="action", required=True)

  owner = sub.add_parser("owner-credentials")
  owner.add_argument("--key", default="owner-credentials")

  run = sub.add_parser("run")
  run.add_argument("--key", default=None,
                   help="Short stable name for the request, e.g. mail-api-key; "
                        "defaults to one derived from the title.")
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
  if args.action in ("run", "owner-credentials"):
    if args.action == "owner-credentials":
      request = {"preset": "owner_credentials"}
    else:
      command = args.command[1:] if args.command[:1] == ["--"] else args.command
      request = {
        "title": args.title, "description": args.description,
        "fields": args.field, "command": command, "cwd": os.getcwd(),
      }
    try:
      key = args.key or _key_from_title(args.title)
      print(json.dumps(_record_secret_request(key, request)))
      return 0
    except SecureInputRefused as exc:
      print(f"{exc} Do not continue as if values were provided.")
      return 1
    except Exception:
      print("Secure input request was not confirmed. Retry the identical request; do not continue as if values were provided.")
      return 1

  spec = {
    "mode": "reveal",
    "title": args.title,
    "description": args.description,
    "fields": args.field,
  }

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
