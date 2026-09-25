"""A chat's contribution lookup must not re-parse the whole ledger per open.

Opening any chat asks for that chat's contributions. The ledger only grows
(thousands of merged records), and parsing every record on each open held
the server's event loop for ~0.25s at a time.
"""

import json
from pathlib import Path

import pytest

from app.routes import github as gh
from app.storage_io import atomic_write

APP_ID = 9901


def _record(record_id, chat_id, *, stack=None, status="open", **extra):
  plan = {"repo": "owner/repo"}
  if stack:
    plan["stack"] = {"id": stack, "position": 0}
  return {
    "id": record_id, "type": "pr", "status": status, "chat_id": chat_id,
    "plan": plan, "updated_at": "2026-09-25T00:00:00Z", **extra,
  }


def _write(directory: Path, record: dict) -> Path:
  path = directory / f"{record['id']}.json"
  atomic_write(path, json.dumps(record))
  return path


@pytest.fixture
def ledger(tmp_path, monkeypatch):
  monkeypatch.setattr(gh, "_chat_ledger_index", {})
  parsed: list[str] = []
  real_read = gh._read_record_tolerant

  def counting_read(path):
    parsed.append(Path(path).stem)
    return real_read(path)

  monkeypatch.setattr(gh, "_read_record_tolerant", counting_read)
  _write(tmp_path, _record("mine", "chat-a", stack="s1"))
  _write(tmp_path, _record("stack-mate", "chat-b", stack="s1"))
  _write(tmp_path, _record("elsewhere", "chat-b", stack="s2"))
  _write(tmp_path, _record("history", "chat-c", status="merged"))
  _write(tmp_path, {"id": "comment", "type": "issue_comment",
                    "status": "open", "chat_id": "chat-a"})
  return tmp_path, parsed


def _lookup(directory: Path, chat_id: str):
  paths = tuple(sorted(directory.glob("*.json")))
  related, records = gh._read_chat_related_records(APP_ID, paths, chat_id)
  return sorted(r["id"] for r in related), [r["id"] for r in records]


def test_chat_lookup_returns_its_records_and_their_stack_mates_only(ledger):
  directory, _parsed = ledger
  related, records = _lookup(directory, "chat-a")
  assert records == ["mine"]
  assert related == ["mine", "stack-mate"]


def test_repeat_chat_lookup_parses_only_matching_records(ledger):
  directory, parsed = ledger
  _lookup(directory, "chat-a")
  parsed.clear()

  _lookup(directory, "chat-a")

  assert sorted(parsed) == ["mine", "stack-mate"]


def test_changed_and_deleted_records_are_seen_on_the_next_lookup(ledger):
  directory, parsed = ledger
  _lookup(directory, "chat-a")
  parsed.clear()

  _write(directory, _record("elsewhere", "chat-b", chat_ids=["chat-a"]))
  (directory / "stack-mate.json").unlink()
  related, records = _lookup(directory, "chat-a")

  assert related == ["elsewhere", "mine"]
  assert sorted(records) == ["elsewhere", "mine"]
  # Only the rewritten record needed a fresh index entry.
  assert parsed.count("elsewhere") == 2  # index refresh + full read
  assert "history" not in parsed
  assert Path(directory / "stack-mate.json") not in gh._chat_ledger_index[APP_ID]
