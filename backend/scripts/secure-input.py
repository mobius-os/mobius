#!/usr/bin/env python3
"""Request transient input and consume it without exposing submitted values."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request


BACKEND_ROOT = Path(__file__).resolve().parent.parent
CONSUMER_TIMEOUT_SECONDS = 120
OWNER_CREDENTIAL_OUTCOMES = {
  0: (True, 0, "Credentials changed. Sign in again with the new details."),
  2: (False, 2, "Credential input was invalid."),
  3: (False, 1, "Owner account was not found."),
  4: (False, 1, "This instance uses managed sign-in."),
  5: (False, 1, "Current password is incorrect."),
  6: (False, 1, "Username must be 1–64 characters."),
  7: (False, 1, "Password cannot be blank or longer than 1024 characters."),
  8: (False, 1, "New passwords do not match."),
  9: (False, 1, "Credentials could not be changed."),
  10: (
    True,
    0,
    "Credentials changed. Sign in again, then restart Möbius to refresh "
    "background access.",
  ),
}
if str(BACKEND_ROOT) not in sys.path:
  sys.path.insert(0, str(BACKEND_ROOT))


def _post(url: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
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
    with urllib.request.urlopen(request, timeout=35) as response:
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
    while True:
      status, state = _post(
        f"{base}/api/secure-inputs/{request_id}/wait",
        {"capability": capability},
      )
      if status >= 300:
        raise RuntimeError("Secure input became unavailable.")
      state_status = state.get("status")
      if state_status == "pending":
        continue
      if state_status != "filled":
        result = state.get("result") or {}
        raise RuntimeError(result.get("message") or "Secure input closed.")
      break
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


def _run_consumer(command: list[str], values: dict[str, str]) -> int:
  if not command:
    raise RuntimeError("A sealed consumer command is required.")
  payload = json.dumps(values, ensure_ascii=False).encode("utf-8")
  try:
    completed = subprocess.run(
      command,
      input=payload,
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      check=False,
      timeout=CONSUMER_TIMEOUT_SECONDS,
    )
  except subprocess.TimeoutExpired:
    return 124
  return completed.returncode


def _consumer_outcome(action: str, returncode: int) -> tuple[bool, int, str]:
  """Map process state to trusted copy without reading consumer output."""
  if action == "owner-credentials":
    return OWNER_CREDENTIAL_OUTCOMES.get(
      returncode,
      (False, 1, "Credentials could not be changed."),
    )
  if returncode == 0:
    return True, 0, "Secure input was consumed without exposing its values."
  if returncode == 124:
    return False, 124, "Sealed consumer timed out; submitted values were discarded."
  return False, returncode or 1, (
    "The sealed consumer failed; submitted values were discarded."
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

  args = parser.parse_args()
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
  request_id = capability = None
  values: dict[str, str] = {}
  try:
    request_id, capability, values = _request_and_consume(spec)
    if args.action == "reveal":
      from app.secure_inputs import build_reveal_envelope
      print(build_reveal_envelope(json.dumps(values, ensure_ascii=False)))
      rc = 0
      ok = True
      message = "Secure values were revealed to the model for this turn only."
    else:
      ok, rc, message = _consumer_outcome(
        args.action,
        _run_consumer(args.command, values),
      )
      print(message)
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
