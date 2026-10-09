"""Lifecycle cards share reply ownership, not one interchangeable event position."""
from app import transcript_rows

from datetime import UTC, datetime, timedelta
import json

import pytest

from app import models
from tests.goal_fixtures import goal_run


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("root_answer", ["visible", "hidden", "missing"])
def test_wait_resume_and_goal_keep_their_event_positions_across_split_pages(
  client, owner_token, db, compact, root_answer,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  base = datetime(2026, 8, 23, 12, tzinfo=UTC)
  ms = int(base.timestamp() * 1000)
  result = "Verified outcome"
  receipt = {
    "type": "tool", "tool": "mobius_control:update_goal",
    "input": json.dumps({"complete": result}),
    "output": "Goal completed, revision 2: 1/1 tasks complete.",
    "status": "done", "output_exit_code": 0, "tool_use_id": "completion",
  }
  messages = [{"role": "user", "content": "Begin", "ts": ms - 1000}]
  if root_answer != "missing":
    messages.append({
      "role": "assistant", "id": "receiving-run", "ts": ms,
      "content": "First reply", "hidden": root_answer == "hidden",
    })
  messages.extend([
    {"role": "assistant", "id": "receiving-run:assistant:1", "ts": ms + 1000,
     "blocks": [{"type": "text", "content": "Verification"}, receipt,
                {"type": "text", "content": "After completion"}]},
    {"role": "assistant", "id": "receiving-run:assistant:2", "ts": ms + 2000,
     "content": "Later split reply"},
    {"role": "assistant", "id": "unrelated", "ts": ms + 6000,
     "content": "Later turn"},
  ])
  created = client.post("/api/chats", json={"title": "Placement", "messages": messages}, headers=auth)
  assert created.status_code == 200, created.text
  chat_id = created.json()["id"]
  db.add(goal_run(db,
    id="receiving-run", chat_id=chat_id, goal_id="placement-goal",
    goal_objective="Verify placement", status="completed", provider="codex",
    started_at=base, ended_at=base + timedelta(seconds=5),
    continuation_json={"reason": "restart"},
  ))
  db.flush()
  goal = db.get(models.ChatGoal, "placement-goal")
  goal.status, goal.result = "completed", result
  db.add(models.ChatWait(
    id="placement-wait", chat_id=chat_id, kind="timer", status="met",
    description="Wait for verification", created_at=base - timedelta(seconds=2),
    deadline_at=base + timedelta(seconds=30), next_check_at=base,
    met_at=base - timedelta(seconds=1), resume_delivered_at=base + timedelta(seconds=4),
  ))
  db.commit()

  query = f"compact={str(compact).lower()}"
  rows = client.get(f"/api/chats/{chat_id}?limit=20&{query}", headers=auth).json()["messages"]
  first_id = "receiving-run" if root_answer == "visible" else "receiving-run:assistant:1"
  assert [m["id"] for m in rows if m.get("continuation_reason")] == [first_id]
  assert [m["id"] for m in rows if m.get("wait_summaries")] == [first_id]
  completed = next(m for m in rows if m.get("id") == "receiving-run:assistant:1")
  blocks = completed["blocks"]
  position = next(i for i, block in enumerate(blocks) if block["type"] == "goal_history")
  assert blocks[position - 1]["tool_use_id"] == "completion"
  assert blocks[position + 1]["content"] == "After completion"
  assert all("goal_summaries" not in m for m in rows)

  later = client.get(f"/api/chats/{chat_id}?limit=2&{query}", headers=auth).json()["messages"]
  assert all(not m.get("continuation_reason") and not m.get("wait_summaries") for m in later)
  assert all(b["type"] != "goal_history" for m in later for b in m.get("blocks", []))
  first_index = next(i for i, m in enumerate(messages) if m.get("id") == first_id)
  earlier = client.get(
    f"/api/chats/{chat_id}?limit=1&before={first_index + 1}&{query}", headers=auth,
  ).json()["messages"][0]
  assert earlier["continuation_reason"] == "restart"
  assert earlier["wait_summaries"][0]["id"] == "placement-wait"
  db.refresh(db.get(models.Chat, chat_id))
  assert list(transcript_rows.history(db.get(models.Chat, chat_id))) == messages
