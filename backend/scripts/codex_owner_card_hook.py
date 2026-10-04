"""End a saved-card turn before Codex resumes from its completed tool.

No transcript is read here. A bounded receipt supplies only a card identity;
the run-bound backend validates that this exact turn saved it. Output remains
unchanged, and the ordinary receipt path still owns cards not seen by a hook.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.owner_card_receipts import owner_card_receipt_id
from mobius_control_mcp import _agent_api_call


def finish_card_tool(payload: dict) -> None:
  if payload.get("hook_event_name") != "PostToolUse" or payload.get("agent_id"):
    return
  question_id = owner_card_receipt_id(payload.get("tool_response"))
  if question_id is None:
    return
  chat_id = os.environ.get("CHAT_ID")
  if not chat_id:
    return
  # Awaited inside PostToolUse: unlike a sink-side background interrupt, the
  # tool cannot resume the model while this request is still in flight.
  _agent_api_call("POST", f"/api/chats/{chat_id}/owner-card-end", {
    "question_id": question_id,
  })


def main() -> None:
  try:
    finish_card_tool(json.load(sys.stdin))
  except Exception as exc:
    # Failure is visible to Codex; the existing completed-receipt interrupt
    # remains available. Never replace the successful card result or claim
    # that an unvalidated receipt stopped a turn.
    print(f"Owner-card hook failed: {exc}", file=sys.stderr)
  print("{}")


if __name__ == "__main__":
  main()
