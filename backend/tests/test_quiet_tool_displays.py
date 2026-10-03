"""Card duplicates disappear without hiding notifications or rewriting history."""
import copy
import json

import pytest

from app.chat_transcript import compact_messages_for_detail, redundant_interaction_tool_indexes
from app.memory_recall import EMPTY_RECALL_BINDING


CARD = {"type": "question", "question_id": "card", "questions": [{"question": "Continue?"}]}


def notice(**extra):
  return {"type": "tool", "tool": "mobius_control:notify_owner", "status": "done",
    "input": json.dumps({"title": "Möbius needs your answer", "body": "Continue?"}),
    "output": "Sent. Tapping it opens /shell/?chat=this-chat.", **extra}


@pytest.mark.parametrize("prefix", ["mobius_control:", "mcp__mobius_control__"])
@pytest.mark.parametrize("before", [True, False])
def test_card_owns_its_successful_notification_in_either_order(prefix, before):
  tool = notice(tool=prefix + "notify_owner")
  blocks = [tool, CARD] if before else [CARD, tool]
  assert redundant_interaction_tool_indexes(blocks, chat_id="this-chat") == {0 if before else 1}


@pytest.mark.parametrize("extra", [
  {"status": "running"}, {"status": "failed"}, {"output_exit_code": 1},
  {"output": json.dumps({"isError": True})},
  {"output": json.dumps({"result": json.dumps({"isError": True})})},
  {"output_exit_code": 0, "output": json.dumps({"isError": True})},
  {"output_exit_code": 0, "output": json.dumps({"result": json.dumps({"isError": True})})},
  {"input": "{}"}, {"input": "{invalid"}, {"input": "[]"},
  {"input": "{invalid, title=Möbius needs your answer, body=Continue?"},
  {"input": 'title=Möbius needs your answer, target=/shell/?app=57, body=See, target=/shell/?chat=this-chat'},
  {"input": 'title=Build finished, body=Context, title=Möbius needs your answer'},
  {"input": '["x, title=Möbius needs your answer, body=x"]'},
  {"input": '[invalid, title=Möbius needs your answer, body=x'},
  {"input": json.dumps({"title": "Build finished"})},
  {"input": json.dumps({"title": "Möbius needs your answer", "target": "/shell/?app=57"})},
  {"input": json.dumps({"title": "Möbius needs your answer", "target": None})},
  {"input": "title=Möbius needs your answer, body=" + "x" * 200},
])
def test_unrelated_running_failed_or_unknown_notifications_stay_visible(extra):
  assert redundant_interaction_tool_indexes([notice(**extra), CARD], chat_id="this-chat") == set()


def test_no_card_means_no_notification_deduplication():
  assert redundant_interaction_tool_indexes([notice()]) == set()


def test_legacy_summary_and_explicit_same_chat_target_have_the_same_meaning():
  legacy = notice(input="title=Möbius needs your answer, body=Continue?")
  explicit = notice(input=json.dumps({"title": "Möbius needs your answer", "target": "/shell/?chat=this-chat"}))
  assert redundant_interaction_tool_indexes([legacy, CARD]) == {0}
  assert redundant_interaction_tool_indexes([explicit, CARD], chat_id="this-chat") == {0}
  assert redundant_interaction_tool_indexes([explicit, CARD], chat_id="different-chat") == set()
  assert redundant_interaction_tool_indexes([explicit, CARD]) == set()


def test_deduplication_precedes_compaction_without_mutating_stored_blocks():
  blocks = [{"type": "tool", "tool": "Read"}, notice(), {"type": "tool", "tool": "Bash"}, CARD]
  messages = [{"role": "assistant", "blocks": blocks}]
  original = copy.deepcopy(messages)
  result = compact_messages_for_detail(messages, message_offset=0, binding=EMPTY_RECALL_BINDING, chat_id="this-chat")
  projected = result[0]["blocks"]
  assert [block.get("tool") for block in projected if block["type"] == "tool"] == ["Read", "Bash"]
  assert projected[-1]["question_id"] == CARD["question_id"]
  assert projected[-1]["questions"] == CARD["questions"]
  assert messages == original


def test_compact_and_expanded_reads_both_omit_only_the_card_notification(client, auth):
  messages = [{"role": "assistant", "blocks": [
    {"type": "tool", "tool": "Read"}, {"type": "tool", "tool": "Bash"}, notice(),
    notice(input=json.dumps({"title": "Build finished"})), CARD,
  ]}]
  response = client.post("/api/chats", headers=auth, json={"title": "Synthetic Q&A notification", "messages": messages})
  assert response.status_code == 200
  chat_id = response.json()["id"]
  compact = client.get(f"/api/chats/{chat_id}?compact=1", headers=auth)
  assert compact.status_code == 200
  tools = []
  for block in compact.json()["messages"][0]["blocks"]:
    if block.get("type") == "activity":
      tools.extend(entry["item"] for entry in block["entries"])
    elif block.get("type") == "tool":
      tools.append(block)
  assert len(tools) == 3
  assert [tool["tool"] for tool in tools].count("mobius_control:notify_owner") == 1
  detail = client.get(f"/api/chats/{chat_id}/activity-detail?message_index=0&start=0&end=5", headers=auth)
  assert detail.status_code == 200
  visible = [entry["item"] for entry in detail.json()["entries"]]
  assert len(visible) == 3
  assert json.loads(visible[-1]["input"])["title"] == "Build finished"


@pytest.mark.parametrize("result", [{"isError": True}, {"result": {"isError": True}}])
def test_compact_and_expanded_reads_keep_semantic_mcp_failure_with_exit_zero(client, auth, result):
  failed = notice(output_exit_code=0, output=json.dumps(result))
  response = client.post("/api/chats", headers=auth, json={
    "title": "Synthetic failed notification", "messages": [{"role": "assistant", "blocks": [failed, CARD]}],
  })
  assert response.status_code == 200
  chat_id = response.json()["id"]
  compact = client.get(f"/api/chats/{chat_id}?compact=1", headers=auth)
  assert compact.status_code == 200
  assert compact.json()["messages"][0]["blocks"][0]["output"] == failed["output"]
  detail = client.get(f"/api/chats/{chat_id}/activity-detail?message_index=0&start=0&end=2", headers=auth)
  assert detail.status_code == 200
  assert detail.json()["entries"][0]["item"]["output"] == failed["output"]
