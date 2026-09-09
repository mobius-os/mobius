"""Inline chat activity is durable, exact-chat scoped, and stably paged."""

from datetime import datetime
import hashlib

from app import models


def _app(db, suffix: str) -> models.App:
  row = models.App(
    slug=f"activity-{suffix}",
    source_dir=f"/tmp/activity-{suffix}",
    name=f"Activity {suffix}",
    description="",
    jsx_source="",
  )
  db.add(row)
  db.flush()
  return row


def _helper(
  db,
  *,
  suffix: str,
  parent_chat_id: str,
  created_at: datetime,
  notify: bool = True,
  attached_source_work_id: str | None = None,
):
  app = _app(db, suffix)
  child_id = f"child-{suffix}"
  db.add(models.Chat(
    id=child_id,
    title=f"Child {suffix}",
    messages=[{
      "role": "assistant",
      "blocks": [{"type": "text", "content": f"result {suffix}"}],
    }],
    created_by_app_id=app.id,
  ))
  db.add(models.Delegation(
    id=f"helper-{suffix}",
    app_id=app.id,
    parent_chat_id=parent_chat_id,
    parent_root_run_id=f"goal-{suffix}",
    task_key=f"task-{suffix}",
    child_chat_id=child_id,
    provider="claude",
    model="claude-sonnet-4-6",
    effort="high",
    scope="read",
    cwd="/data/platform",
    prompt_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
    notify_parent_on_complete=notify,
    source_work_id=attached_source_work_id,
    source_work_status=("completed" if attached_source_work_id else None),
    created_at=created_at,
  ))
  db.add(models.ChatRun(
    id=f"child-run-{suffix}",
    root_run_id=f"child-run-{suffix}",
    chat_id=child_id,
    status="completed",
    provider="claude",
    started_at=created_at,
    ended_at=created_at,
  ))


def test_activity_route_merges_same_time_events_without_sibling_direct_leaks(
  client, auth, db,
):
  stamp = datetime(2026, 9, 9, 2, 15)
  db.add_all([
    models.Chat(id="activity-self", title="Self", messages=[]),
    models.Chat(id="activity-peer", title="Peer", messages=[]),
    models.Chat(id="activity-sibling", title="Sibling", messages=[]),
    models.Chat(id="activity-other", title="Other", messages=[]),
  ])
  db.add(models.ChatRun(
    id="activity-parent-run",
    root_run_id="activity-goal",
    chat_id="activity-self",
    status="completed",
    provider="claude",
    started_at=stamp,
    ended_at=stamp,
  ))
  db.add_all([
    models.AgentCoordinationMessage(
      id="z-visible", room_kind="workspace", room_id="owner",
      from_chat_id="activity-peer", to_chat_id="activity-self",
      kind="finding", delivery="next_turn", body="visible inbound",
      created_at=stamp,
    ),
    models.AgentCoordinationMessage(
      id="a-visible", room_kind="workspace", room_id="owner",
      from_chat_id="activity-self", to_chat_id="activity-peer",
      kind="note", delivery="next_turn", body="visible outbound",
      created_at=stamp,
    ),
    models.AgentCoordinationMessage(
      id="zz-hidden-direct", room_kind="workspace", room_id="owner",
      from_chat_id="activity-sibling", to_chat_id="activity-other",
      kind="note", delivery="next_turn", body="must stay hidden",
      created_at=stamp,
    ),
  ])
  _helper(
    db,
    suffix="same-time",
    parent_chat_id="activity-self",
    created_at=stamp,
    attached_source_work_id="contribution-job-not-owner-goal",
  )
  _helper(
    db,
    suffix="other-parent",
    parent_chat_id="activity-other",
    created_at=stamp,
  )
  db.commit()

  first = client.get(
    "/api/chats/activity-self/activity?limit=2", headers=auth,
  )
  assert first.status_code == 200, first.text
  first_page = first.json()
  assert [item["id"] for item in first_page["events"]] == [
    "peer:z-visible", "peer:a-visible",
  ]
  assert first_page["next_before"]
  assert first_page["events"][0]["message_id"] == "z-visible"
  assert first_page["events"][0]["type"] == "peer_message"

  second = client.get(
    "/api/chats/activity-self/activity",
    params={"limit": 2, "before": first_page["next_before"]},
    headers=auth,
  )
  assert second.status_code == 200, second.text
  second_page = second.json()
  assert [item["id"] for item in second_page["events"]] == [
    "delegation:helper-same-time:completed",
  ]
  helper = second_page["events"][0]
  assert helper == {
    "id": "delegation:helper-same-time:completed",
    "type": "helper_result",
    "created_at": stamp.isoformat(),
    "delegation_id": "helper-same-time",
    "task_key": "task-same-time",
    "status": "completed",
    "body": "result same-time",
    "result_truncated": False,
    "child_chat_id": "child-same-time",
    "source_work_id": "goal-same-time",
    "consumption": "available",
    "display_position": None,
  }
  assert second_page["next_before"] is None
  all_ids = {
    item["id"] for item in [*first_page["events"], *second_page["events"]]
  }
  assert "peer:zz-hidden-direct" not in all_ids
  assert "delegation:helper-other-parent:completed" not in all_ids


def test_historical_inline_helper_is_visible_as_incorporated(client, auth, db):
  stamp = datetime(2026, 9, 9, 3, 0)
  db.add(models.Chat(id="inline-parent", title="Inline", messages=[]))
  _helper(
    db,
    suffix="inline-history",
    parent_chat_id="inline-parent",
    created_at=stamp,
    notify=False,
  )
  db.commit()

  response = client.get("/api/chats/inline-parent/activity", headers=auth)
  assert response.status_code == 200, response.text
  event = response.json()["events"][0]
  assert event["consumption"] == "incorporated"
  assert event["body"] == "result inline-history"


def test_activity_route_requires_exact_chat_access_and_valid_cursor(
  client, auth, db,
):
  db.add(models.Chat(id="activity-auth", title="Authorized", messages=[]))
  db.commit()

  path = "/api/chats/activity-auth/activity"
  assert client.get(path).status_code == 401
  assert client.get(path, params={"before": "not-a-cursor"}, headers=auth).status_code == 422
  assert client.get(path, params={"limit": 101}, headers=auth).status_code == 422
  assert client.get("/api/chats/missing/activity", headers=auth).status_code == 404


def test_helper_settled_hook_records_frontier_once_without_chat_messages(db):
  import asyncio
  from app.broadcast import ChatBroadcast
  from app.chat_activity import chat_activity_page
  from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
  from app.delegations import wake_parent_after_child_settled
  from app.memory_recall import EMPTY_RECALL_BINDING

  parent_id = 'helper-position-parent'
  db.add(models.Chat(id=parent_id, messages=[]))
  _helper(db, suffix='position', parent_chat_id=parent_id,
          created_at=datetime(2026, 9, 9), notify=False)
  db.commit()
  sink = ChatEventSink(ChatBroadcast(parent_id), chat_id=parent_id,
                       recall_binding=EMPTY_RECALL_BINDING)
  register_active_sink(parent_id, sink)
  try:
    sink.publish({'type': 'text', 'content': 'before'})
    asyncio.run(wake_parent_after_child_settled('child-position'))
    sink.publish({'type': 'text', 'content': ' after answer'})
    asyncio.run(wake_parent_after_child_settled('child-position'))
    db.expire_all()
    event = chat_activity_page(db, parent_id)['events'][0]
    assert event['display_position'] == {
      'assistant_message_id': sink.assistant_message_id,
      'block_index': 0, 'text_offset': 6,
    }
    assert db.get(models.Chat, parent_id).messages == []
  finally:
    unregister_active_sink(parent_id, sink)
