"""Save an owner-input card and return its receipt, never wait for an answer.

The saved card ends the turn: the response is cut at the card, so its
`message` carries the turn's closing message. See app/questions.py for the
card lifecycle.

Agents call this through the Möbius control tools. When a provider cannot
surface those tools, the one command-line fallback for every control is
`python3 /data/platform/backend/scripts/mobius_control_mcp.py call <tool>
--args-json '<json>'` (or `--args-json -` to read the JSON from stdin).
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


_SAFE_REJECTION_PATH_PARTS = frozenset({
  "questions", "id", "header", "question", "options",
  "label", "description", "on_answer", "work_key", "message",
})


def request_approval(
  question: str, options: list[dict], work_key: str, message: str | None = None,
) -> dict:
  body = {"question": question, "options": options, "work_key": work_key}
  return save_card("approval", _with_message(body, message))


def request_question(questions: list[dict], message: str | None = None) -> dict:
  return save_card("question", _with_message({"questions": questions}, message))


def request_restart(message: str | None = None) -> dict:
  """Ask the platform to derive and save the exact pending restart action."""
  return save_card("restart-request", _with_message({}, message))


def _with_message(body: dict, message: str | None) -> dict:
  """The turn's closing message, shown above the card it travels with."""
  return {**body, "message": message} if message is not None else body


def _format_rejection_detail(candidate: object) -> str:
  if isinstance(candidate, str):
    return " ".join(candidate.split())[:1000]
  if isinstance(candidate, dict):
    parts = [
      " ".join(value.split())
      for value in (candidate.get("code"), candidate.get("message"))
      if isinstance(value, str) and value.strip()
    ]
    return ": ".join(parts)[:1000]
  if isinstance(candidate, list):
    issues = []
    for issue in candidate[:8]:
      if not isinstance(issue, dict):
        continue
      loc = issue.get("loc")
      message = issue.get("msg")
      if not isinstance(loc, (list, tuple)) or not isinstance(message, str):
        continue
      path = ""
      for part in loc:
        if part == "body":
          continue
        if isinstance(part, int):
          path += f"[{part}]"
          continue
        safe_part = part if part in _SAFE_REJECTION_PATH_PARTS else "<field>"
        path += ("." if path else "") + safe_part
      clean_message = " ".join(message.split())
      issues.append(f"{path}: {clean_message}" if path else clean_message)
    return "; ".join(issues)[:1000]
  return ""


def save_card(kind: str, body: dict) -> dict:
  """Save safe prompts, returning only a receipt, never a human answer."""
  names = ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN")
  values = [os.environ.get(name, "") for name in names]
  if not all(values):
    raise SystemExit("owner input needs the current agent-run environment")
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
      detail = _format_rejection_detail(candidate)
    except (OSError, ValueError, AttributeError):
      pass
    suffix = f": {detail}" if detail else ""
    retry = (
      " Retry the identical request to recover its saved receipt."
      if exc.code >= 500 else " Fix the stated conflict before trying again."
    )
    raise SystemExit(
      f"Could not save owner-input card ({exc.code}){suffix}. "
      f"No answer or approval was granted.{retry}"
    ) from exc
  except (URLError, TimeoutError, ValueError) as exc:
    raise SystemExit(
      "Owner-input card save was not confirmed. No answer or approval was granted; "
      "retry the identical request to recover its saved receipt."
    ) from exc
  if (kind == "approval" and isinstance(payload, dict)
      and payload.get("state") in {"held_by_peer", "completed"}
      and isinstance(payload.get("work_key"), str)):
    # The approval's work_key is already owned or completed elsewhere: no card
    # was saved, the turn continues, and this is the claim_agent_work result.
    return payload
  if (not isinstance(payload, dict)
      or payload.get("state") not in {"waiting_for_owner", "answered"}
      or not isinstance(payload.get("question_id"), str)
      or not payload["question_id"]
      or not isinstance(payload.get("next_action"), str)):
    raise SystemExit(
      "Invalid owner-input card receipt; no answer or approval was granted."
    )
  return payload
