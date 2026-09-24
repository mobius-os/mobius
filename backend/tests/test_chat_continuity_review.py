"""Independent review cases for the continuity failure and token-cost boundary."""

import os
from pathlib import Path

from app import auth as authentication, models
from app.chat_continuity import (
  completed_prefix, delivered_input_prefix, verified_uncovered_messages,
)
from app.chat_writer import (
  AdmitProviderExecution, CheckpointContinuity, RecordDeliveredInput,
  StartTurn, get_writer,
)
from app.routes import chat_continuity as routes


def _agent(chat):
  result = get_writer().submit(StartTurn(
    chat_id=chat.id, run_token="review-run",
    user_msg={"role": "user", "content": "New work", "ts": 10},
    title_source="New work",
  )).result(timeout=5)
  assert isinstance(result, dict)
  return {"Authorization": "Bearer " + authentication.create_agent_token(
    chat.id, "test", 0, run_id="review-run",
  )}


def _save(client, headers, checkpoint="first", **extra):
  return client.post("/api/chat/continuity/checkpoints", headers=headers, json={
    "checkpoint_id": checkpoint,
    "digest": "New milestone, not a claim about earlier uncovered work.",
    **extra,
  })


def _note(chat):
  return Path(os.environ["DATA_DIR"]) / "shared/memory/chats" / chat.id / "index.md"


def test_legacy_read_does_not_migrate_or_rewrite(client, auth, chat, db):
  path = _note(chat)
  path.parent.mkdir(parents=True)
  original = "---\ndescription: Old title\n---\n## Digest\nSmall state\n## Summary\nLong history\n"
  path.write_text(original)
  stamp = path.stat().st_mtime_ns
  response = client.get(f"/api/chats/{chat.id}/continuity", headers=auth)
  assert response.status_code == 200
  assert response.json()["summary"] == "Small state"
  assert "legacy_markdown" not in response.json()["entries"][0]
  assert db.get(models.ChatContinuity, chat.id) is None
  assert path.read_text() == original
  assert path.stat().st_mtime_ns == stamp


def test_missing_delivery_preserves_coverage_and_retry_is_current(
  client, chat, db,
):
  # Fixture-only source setup; live writes use the actor.
  chat.messages = [
    {"role": "user", "content": "Important earlier requirement", "ts": 1},
    {"role": "assistant", "content": "Earlier answer", "id": "earlier-run", "ts": 2},
  ]
  db.commit()
  headers = _agent(chat)
  first = _save(client, headers)
  assert first.status_code == 200, first.text
  assert first.json()["coverage"]["message_count"] == 0
  read = client.get("/api/chat/continuity?after_revision=1", headers=headers).json()
  assert read["entries"] == []
  assert "source_cursor" not in read
  assert read["coverage"]["message_count"] == 0
  db.expire_all()
  source = list(db.get(models.Chat, chat.id).messages or [])
  count, proof = delivered_input_prefix(source, "review-run", "New work")
  assert count == 3 and proof
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token="review-run",
  )).result(timeout=5)
  assert get_writer().submit(RecordDeliveredInput(
    chat_id=chat.id, run_token="review-run",
    message_count=count, prefix_hash=proof,
  )).result(timeout=5)
  second = _save(client, headers, "catch-up",
    digest="Caught up: preserve the earlier requirement.",
    summary="Working with the earlier requirement.")
  assert second.status_code == 200, second.text
  assert second.json()["coverage"]["message_count"] == 3
  retry = _save(client, headers).json()
  assert retry["entry_revision"] == 1
  assert retry["revision"] == 2
  assert retry["coverage"] == second.json()["coverage"]
  assert not {"summary", "title", "entries", "digest"} & retry.keys()
  changed = _save(client, headers, digest="Different content with the same identity")
  assert changed.status_code == 409
  assert changed.json()["detail"]["reason"] == "checkpoint_mismatch"


def test_source_cursor_is_no_longer_a_checkpoint_input(client, chat, db):
  headers = _agent(chat)
  result = _save(client, headers, source_cursor={
    "message_count": 1, "prefix_hash": "0" * 64,
  })
  assert result.status_code == 422
  db.expire_all()
  assert db.get(models.ChatContinuity, chat.id) is None
  assert db.query(models.ChatContinuityEntry).count() == 0
  assert not _note(chat).exists()


def test_sdk_accepted_prefix_excludes_later_steer_and_current_output(
  client, chat, db,
):
  headers = _agent(chat)
  db.expire_all()
  accepted = list(db.get(models.Chat, chat.id).messages or [])
  count, proof = delivered_input_prefix(accepted, "review-run", "New work")
  assert count == 1 and proof
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token="review-run",
  )).result(timeout=5)
  assert get_writer().submit(RecordDeliveredInput(
    chat_id=chat.id, run_token="review-run",
    message_count=count, prefix_hash=proof,
  )).result(timeout=5)

  # Fixture simulates an accepted mid-turn steer and a mutable assistant row;
  # the checkpoint must not infer either from the newest transcript snapshot.
  db.expire_all()
  current = db.get(models.Chat, chat.id)
  current.messages = list(current.messages or []) + [
    {"role": "assistant", "id": "review-run:assistant:1", "content": "partial"},
    {"role": "user", "content": "late correction"},
  ]
  db.commit()
  saved = _save(client, headers, summary="Initial task accepted.")
  assert saved.status_code == 200, saved.text
  assert saved.json()["coverage"] == {
    "message_count": count, "prefix_hash": proof,
  }
  db.expire_all()
  suffix, verified = verified_uncovered_messages(
    db.get(models.Chat, chat.id).messages,
    covered_count=count, covered_prefix_hash=proof,
  )
  assert verified and [row["content"] for row in suffix] == [
    "partial", "late correction",
  ]


def test_changed_delivered_prefix_does_not_advance_checkpoint(client, chat, db):
  headers = _agent(chat)
  db.expire_all()
  count, proof = delivered_input_prefix(
    db.get(models.Chat, chat.id).messages, "review-run", "New work",
  )
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token="review-run",
  )).result(timeout=5)
  assert get_writer().submit(RecordDeliveredInput(
    chat_id=chat.id, run_token="review-run",
    message_count=count, prefix_hash=proof,
  )).result(timeout=5)
  db.expire_all()
  current = db.get(models.Chat, chat.id)
  changed = list(current.messages)
  changed[0] = {**changed[0], "content": "edited after delivery"}
  current.messages = changed
  db.commit()
  saved = _save(client, headers)
  assert saved.status_code == 200, saved.text
  assert saved.json()["coverage"] == {
    "message_count": 0, "prefix_hash": None,
  }


def test_delivered_input_requires_admission_and_live_run(chat, db):
  _agent(chat)
  db.expire_all()
  count, proof = delivered_input_prefix(
    db.get(models.Chat, chat.id).messages, "review-run", "New work",
  )
  command = lambda: RecordDeliveredInput(
    chat_id=chat.id, run_token="review-run",
    message_count=count, prefix_hash=proof,
  )
  assert get_writer().submit(command()).result(timeout=5) is False
  db.expire_all()
  assert db.get(models.ChatRun, "review-run").delivered_message_count is None
  get_writer().submit(AdmitProviderExecution(
    chat_id=chat.id, run_token="review-run",
  )).result(timeout=5)
  assert get_writer().submit(command()).result(timeout=5) is True
  assert get_writer().submit(command()).result(timeout=5) is True

  # A newer run cannot be overwritten by a late callback from this one.
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token="successor-run",
    user_msg={"role": "user", "content": "next", "ts": 20},
    title_source="next",
  )).result(timeout=5)
  assert get_writer().submit(command()).result(timeout=5) is False


def test_projection_failure_keeps_commit_and_identical_retry_repairs(
  client, chat, db, monkeypatch,
):
  headers = _agent(chat)
  project = routes._project_from_fresh_session

  def fail(*args):
    raise OSError("fixture disk unavailable")

  monkeypatch.setattr(routes, "_project_from_fresh_session", fail)
  response = _save(client, headers, summary="Durable despite projection failure")
  assert response.status_code == 200, response.text
  assert response.json()["projection_warning"]
  db.expire_all()
  assert db.get(models.ChatContinuity, chat.id).revision == 1
  monkeypatch.setattr(routes, "_project_from_fresh_session", project)
  retry = _save(client, headers, summary="Durable despite projection failure")
  assert retry.status_code == 200, retry.text
  assert retry.json()["status"] == "already_committed"
  assert "projection_warning" not in retry.json()
  assert "Durable despite projection failure" in _note(chat).read_text()
  db.expire_all()
  assert db.query(models.ChatContinuityEntry).count() == 1


def test_current_segments_and_late_steers_stay_uncovered():
  source = [
    {"role": "user", "content": "old"},
    {"role": "assistant", "id": "old-run:assistant:1", "content": "settled"},
    {"role": "user", "content": "new"},
    {"role": "assistant", "id": "review-run:assistant:1", "content": "partial"},
    {"role": "user", "content": "new correction"},
    {"role": "assistant", "id": "review-run:assistant:2", "content": "continued"},
  ]
  boundary, proof = completed_prefix(source, "review-run")
  assert boundary == 2
  source[-1]["content"] += " more text"
  assert completed_prefix(source, "review-run") == (boundary, proof)
  source[1]["content"] += " changed prior fact"
  assert completed_prefix(source, "review-run")[1] != proof


def test_concurrent_commands_receive_serial_platform_revisions(chat, db):
  _agent(chat)
  futures = [get_writer().submit(CheckpointContinuity(
    chat_id=chat.id, run_token="review-run", checkpoint_id=identity,
    digest=identity,
  )) for identity in ("branch-a", "branch-b")]
  results = [future.result(timeout=5) for future in futures]
  assert [result["status"] for result in results] == ["committed", "committed"]
  db.expire_all()
  assert db.get(models.ChatContinuity, chat.id).revision == 2
  assert db.query(models.ChatContinuityEntry).count() == 2


def test_bounded_reads_do_not_fetch_full_historical_baseline(client, auth, chat, db):
  from sqlalchemy import event
  from app.database import engine

  headers = _agent(chat)
  path = _note(chat)
  path.parent.mkdir(parents=True)
  path.write_text("## Digest\nOld state\n## Summary\n" + "old details " * 10_000)
  assert _save(client, headers).status_code == 200
  statements = []

  def capture(conn, cursor, statement, parameters, context, executemany):
    if "chat_continuity_entries" in statement:
      statements.append(statement)

  event.listen(engine, "before_cursor_execute", capture)
  try:
    response = client.get(f"/api/chats/{chat.id}/continuity?limit=1", headers=auth)
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  assert response.status_code == 200
  result = response.json()
  assert len(result["entries"]) == 1
  assert result["has_older"] is True
  assert result["has_more"] is False
  assert "legacy_markdown" not in response.text
  assert statements
  assert all("legacy_markdown" not in statement for statement in statements)
  assert all("LIMIT" in statement for statement in statements)


def test_owner_browser_cannot_impersonate_run_for_checkpoint(client, auth, chat):
  result = _save(client, auth)
  assert result.status_code in {401, 403}


def test_authoritative_sibling_summary_does_not_read_growing_projection(chat, db, monkeypatch):
  from app import memory

  db.add(models.ChatContinuity(
    chat_id=chat.id, revision=1, current_summary="Only the current paragraph.",
    covered_message_count=0,
  ))
  db.commit()
  path = _note(chat)
  path.parent.mkdir(parents=True)
  path.write_text("Stale projection should not be read")

  def fail(path):
    raise AssertionError("DB-authoritative startup read the full journal file")

  monkeypatch.setattr(memory, "_read", fail)
  block = memory.build_memory_block(
    os.environ["DATA_DIR"], ordered_chat_ids=[chat.id],
    continuity_by_chat_id=memory.recent_continuity_metadata(db, [chat.id]),
  )
  assert "Only the current paragraph." in block.text
  assert "Status: idle" in block.text
  assert "Snapshot:" in block.text


def test_legacy_sibling_retains_paragraph_alongside_runtime_snapshot(chat, db):
  from app import memory

  path = _note(chat)
  path.parent.mkdir(parents=True)
  path.write_text("---\ndescription: Prior title\n---\n## Digest\nPrior short state.\n## Summary\nDo not inject this long history.")
  block = memory.build_memory_block(
    os.environ["DATA_DIR"], ordered_chat_ids=[chat.id],
    continuity_by_chat_id=memory.recent_continuity_metadata(db, [chat.id]),
  )
  assert "Prior short state." in block.text
  assert "Do not inject" not in block.text
  assert "Status: idle" in block.text
  assert "Snapshot:" in block.text


def test_sibling_status_uses_latest_run_not_stale_nonterminal_predecessor(chat, db):
  from datetime import datetime
  from app import memory

  db.add_all([
    models.ChatRun(id="old", chat_id=chat.id, status="running", started_at=datetime(2026, 1, 1)),
    models.ChatRun(id="new", chat_id=chat.id, status="completed", started_at=datetime(2026, 1, 2)),
  ])
  db.commit()
  item = memory.recent_continuity_metadata(db, [chat.id])[chat.id]
  assert item["status"] == "idle"


def test_versioned_projection_read_without_db_preserves_history_and_short_state():
  from app.chat_continuity import legacy_parts
  from app.chat_notes import extract_cumulative_summary

  projection = (
    "---\ncontinuity_version: 2\ndescription: Restored title\n---\n"
    "## Summary\nCurrent paragraph.\n## Digest\n### Revision 1\nFirst fact.\n"
    "## Historical baseline (legacy raw)\n````markdown\n## Summary\nOld fact.\n````\n"
  )
  name, paragraph, history = legacy_parts(projection)
  assert name == "Restored title"
  assert paragraph == "Current paragraph."
  assert "First fact." in history
  assert "Old fact." in history
  old = "## Summary\nFirst fact.\n## Digest\nA nested legacy heading.\n## Related\nExcluded."
  assert "A nested legacy heading." in extract_cumulative_summary(old)
  assert "Excluded." not in extract_cumulative_summary(old)


def test_small_provider_handoff_uses_db_journal_without_summary_model(
  client, auth, chat, db, monkeypatch,
):
  from app import compaction

  chat.messages = [{"role": "user", "content": "Unsummarized new constraint", "ts": 1}]
  db.add(models.ChatContinuity(
    chat_id=chat.id, revision=1, current_summary="Current brief.",
    covered_message_count=0,
  ))
  db.add(models.ChatContinuityEntry(
    chat_id=chat.id, revision=1, checkpoint_id="prior",
    digest="Important saved decision.", covered_message_count=0,
  ))
  db.commit()
  path = _note(chat)
  path.parent.mkdir(parents=True)
  path.write_text("## Summary\nStale file must not win.")
  monkeypatch.setattr("app.providers.CodexProvider.check_auth", lambda *args: None)

  async def forbidden(*args, **kwargs):
    raise AssertionError("small handoff spawned a summary model")

  monkeypatch.setattr(compaction, "summarize_chat", forbidden)
  response = client.post(f"/api/chats/{chat.id}/provider-switch", headers=auth, json={
    "switch_id": "direct-review", "provider": "codex",
    "agent_settings_json": {
      "model": "gpt-5.4", "effort": "high",
      "effort_by_provider": {"codex": "high"},
    },
  })
  assert response.status_code == 200, response.text
  brief = response.json()["summary"]
  assert "Current brief." in brief
  assert "Important saved decision." in brief
  assert "Unsummarized new constraint" in brief
  assert "Stale file" not in brief


def test_first_post_switch_checkpoint_preserves_idless_marker_and_new_input():
  messages = [
    {"role": "user", "content": "earlier requirement"},
    {"role": "assistant", "id": "prior-run", "content": "settled plan"},
    {"role": "assistant", "kind": "compaction", "content": "portable handoff"},
    {"role": "user", "content": "continue on new provider"},
  ]
  count, proof = delivered_input_prefix(messages, "switch-run", messages[-1]["content"])
  assert count == 2
  suffix, verified = verified_uncovered_messages(
    messages, covered_count=count, covered_prefix_hash=proof,
  )
  assert verified and suffix == messages[2:]
  # Once an ordinary assistant turn settles, the next input has a proven
  # predecessor. No marker-specific inference or special-case ack is needed.
  messages.extend([
    {"role": "assistant", "id": "switch-run", "content": "continued plan"},
    {"role": "user", "content": "next refinement"},
  ])
  count, _proof = delivered_input_prefix(messages, "next-run", "next refinement")
  assert count == len(messages)
