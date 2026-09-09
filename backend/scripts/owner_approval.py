#!/usr/bin/env python3
"""Save an owner approval card and return its receipt, never wait for an answer."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request_approval(
  question: str, options: list[dict], work_key: str,
) -> dict:
  body = {"question": question, "options": options, "work_key": work_key}
  return save_card("approval", body)


def request_question(questions: list[dict]) -> dict:
  return save_card("question", {"questions": questions})


def request_restart() -> dict:
  """Ask the platform to derive and save the exact pending restart action."""
  return save_card("restart-request", {})


def save_card(kind: str, body: dict) -> dict:
  """Save safe prompts, returning only a receipt, never a human answer."""
  names = ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN")
  values = [os.environ.get(name, "") for name in names]
  if not all(values):
    raise SystemExit("owner approval needs the current agent-run environment")
  base, token, chat_id, _run_id = values
  request = Request(
    f"{base.rstrip('/')}/api/chats/{quote(chat_id, safe='')}/{kind}",
    data=json.dumps(body).encode("utf-8"),
    method="POST",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
  )
  try:
    # Only the save has a transport deadline. Human answers never live in
    # this request, process, or tool connection.
    with urlopen(request, timeout=35) as response:
      payload = json.loads(response.read())
  except HTTPError as exc:
    detail = ""
    try:
      raw = exc.read(4096)
      parsed = json.loads(raw.decode("utf-8", errors="replace"))
      candidate = parsed.get("detail") if isinstance(parsed, dict) else None
      if isinstance(candidate, str):
        detail = " ".join(candidate.split())[:1000]
    except (OSError, ValueError, AttributeError):
      pass
    suffix = f": {detail}" if detail else ""
    retry = (
      " Retry the identical request to recover its saved receipt."
      if exc.code >= 500 else " Fix the stated conflict before trying again."
    )
    raise SystemExit(
      f"Could not save approval ({exc.code}){suffix}. "
      f"No approval was granted.{retry}"
    ) from exc
  except (URLError, TimeoutError, ValueError) as exc:
    raise SystemExit(
      "Approval save was not confirmed. No approval was granted; "
      "retry the identical request to recover its saved receipt."
    ) from exc
  if (not isinstance(payload, dict)
      or payload.get("state") not in {"waiting_for_owner", "answered"}
      or not isinstance(payload.get("question_id"), str)
      or not payload["question_id"]
      or not isinstance(payload.get("next_action"), str)):
    raise SystemExit("Invalid approval receipt; no approval was granted.")
  return payload


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("question", nargs="?")
  parser.add_argument("--questions-json", help="JSON array for a saved ordinary question card")
  parser.add_argument(
    "--restart", action="store_true",
    help="save a platform-owned card for the exact pending server restart",
  )
  parser.add_argument("--option", action="append", nargs=2,
                      metavar=("LABEL", "DESCRIPTION"))
  parser.add_argument(
    "--work-key", help="required stable identity for the action awaiting approval",
  )
  args = parser.parse_args()
  if args.restart:
    if args.question or args.questions_json is not None or args.option or args.work_key:
      parser.error("--restart does not accept approval or question arguments")
    print(json.dumps(request_restart()))
  elif args.questions_json is not None:
    if args.question or args.option or args.work_key:
      parser.error("use either --questions-json or an approval question with --option")
    try:
      questions = json.loads(args.questions_json)
    except ValueError:
      parser.error("--questions-json must be a JSON array")
    print(json.dumps(request_question(questions)))
  else:
    if not args.question or not args.option or not args.work_key:
      parser.error("approval needs a question, --work-key, and --option choices")
    print(json.dumps(request_approval(args.question, [
      {"label": label, "description": description}
      for label, description in args.option
    ], work_key=args.work_key)))


if __name__ == "__main__":
  main()
