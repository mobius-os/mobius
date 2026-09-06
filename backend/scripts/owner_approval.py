#!/usr/bin/env python3
"""Save an owner approval card and return its receipt, never wait for an answer."""

from __future__ import annotations

import argparse
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def request_approval(question: str, options: list[dict]) -> dict:
  return save_card("approval", {"question": question, "options": options})


def request_question(questions: list[dict]) -> dict:
  return save_card("question", {"questions": questions})


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
    raise SystemExit(
      f"Could not save approval ({exc.code}); no approval was granted. "
      "Check the open card or retry the identical request."
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
  parser.add_argument("--option", action="append", nargs=2,
                      metavar=("LABEL", "DESCRIPTION"))
  args = parser.parse_args()
  if args.questions_json is not None:
    if args.question or args.option:
      parser.error("use either --questions-json or an approval question with --option")
    try:
      questions = json.loads(args.questions_json)
    except ValueError:
      parser.error("--questions-json must be a JSON array")
    print(json.dumps(request_question(questions)))
  else:
    if not args.question or not args.option:
      parser.error("approval needs a question and --option choices")
    print(json.dumps(request_approval(args.question, [
      {"label": label, "description": description}
      for label, description in args.option
    ])))


if __name__ == "__main__":
  main()
