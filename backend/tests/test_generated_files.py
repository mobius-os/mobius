# backend/tests/test_generated_files.py
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import chat as chat_mod
from app import generated_files as gf
from app import models
from app.broadcast import ChatBroadcast
from app.chat_retention import purge_expired_chat_tombstones
from app.config import get_settings
from app.memory_recall import EMPTY_RECALL_BINDING


def _write_row(db, chat, *, name, path, size=11, mime_type="application/pdf"):
  row = models.GeneratedFile(
    chat_id=chat.id, name=name, path=path, size=size, mime_type=mime_type,
  )
  db.add(row)
  db.commit()
  return row


def test_serve_generated_file_by_recorded_name(client, db, auth, chat):
  """The route serves the exact path recorded for THIS chat's own row, never
  a client-supplied filesystem path — see routes/generated_files.py."""
  settings = get_settings()
  (Path(settings.data_dir) / "report.pdf").write_bytes(b"%PDF-1.4 fake")
  _write_row(db, chat, name="report.pdf", path="report.pdf")

  media_token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/report.pdf",
    params={"token": media_token},
  )
  assert res.status_code == 200
  assert res.content == b"%PDF-1.4 fake"
  disposition = res.headers["content-disposition"]
  assert disposition.startswith('attachment; filename="report.pdf"')
  assert "filename*=UTF-8''report.pdf" in disposition


def test_serve_generated_file_from_private_output_dir(client, db, auth, chat):
  settings = get_settings()
  directory = gf.output_dir(settings.data_dir, chat.id, create=True)
  (directory / "private.pdf").write_bytes(b"%PDF-private")
  relative = (
    (directory / "private.pdf").relative_to(settings.data_dir).as_posix()
  )
  _write_row(db, chat, name="private.pdf", path=relative)

  token = client.post(
    f"/api/chats/{chat.id}/media-token", headers=auth,
  ).json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/private.pdf",
    params={"token": token},
  )
  assert res.status_code == 200
  assert res.content == b"%PDF-private"


def test_serve_generated_file_unknown_name_404s(client, auth, chat):
  """A name never recorded for this chat 404s — it is never resolved against
  cwd on the fly, so an unrecorded filename can't be probed for."""
  media_token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/never-recorded.pdf",
    params={"token": media_token},
  )
  assert res.status_code == 404


def test_serve_generated_file_rejects_traversal_in_recorded_path(client, db, auth, chat):
  """Even a recorded row can't point outside cwd — defense in depth on top
  of the by-name lookup, matching validate_path_within_base elsewhere."""
  _write_row(db, chat, name="evil.txt", path="../outside.txt")

  media_token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/evil.txt",
    params={"token": media_token},
  )
  assert res.status_code == 400


def test_serve_generated_file_does_not_leak_across_chats(
  client, db, auth, chat, second_chat,
):
  """A file recorded for one chat is invisible through another chat's id —
  the lookup is scoped by chat_id, not just by name."""
  settings = get_settings()
  (Path(settings.data_dir) / "shared-name.csv").write_bytes(b"a,b,c\n")
  _write_row(db, chat, name="shared-name.csv", path="shared-name.csv", mime_type="text/csv")

  media_token_r = client.post(f"/api/chats/{second_chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{second_chat.id}/generated-files/shared-name.csv",
    params={"token": media_token},
  )
  assert res.status_code == 404


def test_colliding_basename_chip_links_to_its_own_file(db, chat):
  """Regression: two files with the same basename (e.g. `report.pdf`
  regenerated in a fresh subfolder) must not both display/link the SAME
  name. `RecordGeneratedFile` suffixes `name` on collision, but that
  suffix has to reach the transcript chip too — otherwise the second
  file's chip silently links to the first file's DB row (same name, same
  download URL) and the actual second file becomes unreachable from the
  UI. `publish_generated_file` awaits the write and rewrites the event's
  `name` to what was actually persisted before it reaches the transcript;
  this proves that round-trip end to end."""
  bc = ChatBroadcast(chat.id)
  sink = chat_mod._ChatEventSink(
    bc, chat.id, run_token="rt-gf", recall_binding=EMPTY_RECALL_BINDING,
  )
  sink.publish({"type": "tool_start", "tool": "Bash", "input": "", "tool_use_id": "t1"})
  sink.publish({"type": "tool_start", "tool": "Bash", "input": "", "tool_use_id": "t2"})

  async def go():
    await sink.publish_generated_file({
      "type": "generated_file", "tool_use_id": "t1",
      "name": "report.pdf", "path": "a/report.pdf",
      "size": 10, "mime_type": "application/pdf",
    })
    await sink.publish_generated_file({
      "type": "generated_file", "tool_use_id": "t2",
      "name": "report.pdf", "path": "b/report.pdf",
      "size": 20, "mime_type": "application/pdf",
    })

  asyncio.run(go())

  first = next(b for b in sink.assistant_blocks if b.get("tool_use_id") == "t1")
  second = next(b for b in sink.assistant_blocks if b.get("tool_use_id") == "t2")
  first_name = first["generated_files"][0]["name"]
  second_name = second["generated_files"][0]["name"]

  assert first_name == "report.pdf"
  assert second_name == "report_1.pdf"
  assert second_name != first_name

  # Each displayed name must resolve, via the download route's lookup, to
  # the exact path ITS OWN tool call produced — not the other one's.
  rows = {
    row.name: row.path for row in db.query(models.GeneratedFile).filter(
      models.GeneratedFile.chat_id == chat.id,
    ).all()
  }
  assert rows[first_name] == "a/report.pdf"
  assert rows[second_name] == "b/report.pdf"


def test_hard_purge_removes_generated_file_rows(db, chat):
  """Regression: GeneratedFile was missing from purge_expired_chat_tombstones'
  dependent_models list, so an 8-day hard purge deleted the Chat row while
  leaving its generated_files row behind — an orphan on SQLite, and a row
  that can block the Chat delete outright once a deployment enforces the
  chat_id foreign key (e.g. PostgreSQL)."""
  _write_row(db, chat, name="report.pdf", path="report.pdf")
  chat_id = chat.id
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  purge_expired_chat_tombstones(db)

  assert db.get(models.Chat, chat_id) is None
  assert db.query(models.GeneratedFile).filter(
    models.GeneratedFile.chat_id == chat_id,
  ).first() is None


def test_diff_excludes_another_chats_own_namespace():
  """Regression: ordinary chats share cwd == settings.data_dir, so a naive
  directory diff can observe a DIFFERENT chat's own files (its uploads, its
  memory state) and misattribute them to whichever chat's turn happens to
  run next — that chat's scoped download route would then serve someone
  else's file. A path under a known DIFFERENT chat's `chats/<id>/...`
  namespace must never become a candidate for this chat, while this chat's
  OWN namespace and ordinary shared-root files remain attributable."""
  before = {}
  after = {
    "report.pdf": ("2026-01-01T00:00:00", 100),
    "chats/other-chat-id/uploads/private-report.pdf": ("2026-01-01T00:00:00", 200),
    "chats/my-chat-id/uploads/mine.pdf": ("2026-01-01T00:00:00", 150),
    "shared/memory/chats/other-chat-id/leak.pdf": ("2026-01-01T00:00:00", 50),
  }
  diff = gf.diff_new_or_changed(
    gf.Snapshot(files=before, complete=True),
    gf.Snapshot(files=after, complete=True),
    own_chat_id="my-chat-id",
  )
  paths = {entry["path"] for entry in diff}

  assert "chats/other-chat-id/uploads/private-report.pdf" not in paths
  assert "shared/memory/chats/other-chat-id/leak.pdf" not in paths
  assert "report.pdf" in paths
  assert "chats/my-chat-id/uploads/mine.pdf" in paths


# --- detection must cover EVERY tool surface that can write a file ---------
#
# Regression: the diff window was originally opened only around the classic
# shell / apply_patch surfaces. An agent whose shell runs through another
# surface (a dynamic/namespaced `exec_command`-style tool, or an MCP server's
# tool) wrote its PDF with no window open around the call, so nothing was ever
# detected and no download chip appeared — the feature silently no-op'd for
# those agents while looking fine for the ones using the classic surface.


def test_codex_detects_every_file_writing_item_type():
  """Every Codex thread-item type that runs caller logic must open a diff
  window, not just the classic shell/apply_patch pair."""
  from app import codex_sdk_runner

  assert set(codex_sdk_runner._FILE_WRITING_ITEM_KEYS) == {
    "CommandExecutionThreadItem",
    "FileChangeThreadItem",
    "McpToolCallThreadItem",
    "DynamicToolCallThreadItem",
  }


def test_codex_can_write_files_matches_by_type_not_tool_name():
  """`_can_write_files` gates on the SDK's item type, so a shell arriving as
  a dynamic/namespaced tool (`exec_command`) is covered without the detector
  needing to know that tool's name."""
  from app import codex_sdk_runner

  class _Dynamic:
    pass

  class _Command:
    pass

  class _WebSearch:
    pass

  sdk = {
    "CommandExecutionThreadItem": _Command,
    "FileChangeThreadItem": type("_FileChange", (), {}),
    "McpToolCallThreadItem": type("_Mcp", (), {}),
    "DynamicToolCallThreadItem": _Dynamic,
  }

  assert codex_sdk_runner._can_write_files(_Dynamic(), sdk) is True
  assert codex_sdk_runner._can_write_files(_Command(), sdk) is True
  # A read-only surface must NOT open a window (no scan per search result).
  assert codex_sdk_runner._can_write_files(_WebSearch(), sdk) is False


def test_claude_matcher_covers_mcp_and_notebook_writes():
  """The Claude hook matcher must cover every tool that can write a file —
  an MCP server's tool runs arbitrary caller logic just like Bash does."""
  from app import claude_sdk_runner

  matcher = claude_sdk_runner._FILE_WRITING_TOOL_MATCHER
  for tool in ("Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"):
    assert tool in matcher
    assert claude_sdk_runner._can_write_generated_file(tool) is True
  assert "mcp__" in matcher
  assert (
    claude_sdk_runner._can_write_generated_file("mcp__local__render") is True
  )
  for tool in ("Agent", "Task", "Workflow", "Read", "WebSearch"):
    assert claude_sdk_runner._can_write_generated_file(tool) is False


def test_output_snapshot_cannot_see_another_chat(tmp_path):
  mine = gf.output_dir(str(tmp_path), "mine", create=True)
  theirs = gf.output_dir(str(tmp_path), "theirs", create=True)
  (mine / "mine.pdf").write_bytes(b"mine")
  (theirs / "theirs.pdf").write_bytes(b"theirs")

  snap = gf.snapshot_output_dir(str(tmp_path), chat_id="mine")

  assert set(snap.files) == {"chats/mine/generated/mine.pdf"}


# --- snapshot completeness: a partial scan must never publish -------------
#
# Regression: snapshot() reused workspace_files.list_entries, whose LIST_LIMIT
# caps a *display* listing at 1000 entries and reports the cut via `truncated`
# — which the detector discarded. On any data_dir past that cap the diff went
# silently partial: real deliverables fell outside the window, and a
# pre-existing unrelated file could ENTER the window between the two scans and
# be recorded (and served) as this chat's deliverable.


def test_snapshot_reports_completeness(tmp_path):
  (tmp_path / "report.pdf").write_bytes(b"%PDF-1.4")
  snap = gf.snapshot(str(tmp_path), own_chat_id="c1")
  assert snap.complete is True
  assert "report.pdf" in snap.files


def test_snapshot_marks_itself_incomplete_past_the_scan_ceiling(tmp_path, monkeypatch):
  monkeypatch.setattr(gf, "MAX_SCAN_ENTRIES", 2)
  for i in range(6):
    (tmp_path / f"f{i}.pdf").write_bytes(b"%PDF-1.4")
  snap = gf.snapshot(str(tmp_path), own_chat_id="c1")
  assert snap.complete is False


def test_baseline_before_tool_detects_fast_command():
  """Regression: the diff baseline must be taken BEFORE the tool runs so
  a fast command whose output file appears BEFORE the baseline is captured
  is still detected.

  This pins the invariant that the baseline (``before``) must never contain
  a file the tool produced — if it does, ``diff_new_or_changed`` sees
  before[path] == after[path] and produces an empty diff, silently dropping
  the chip.

  For the Claude SDK runner the baseline is captured in ``keepalive_hook``
  (matcher=None, the hook whose ``continue_: True`` the CLI awaits before
  starting the tool) — NOT in ``acquire_cwd_reservation_hook`` (matcher=
  "Write|...") which fires concurrently with the tool after the CLI already
  received the first hook's approval.

  For the Codex runner the baseline is captured eagerly at turn start
  because the Codex subprocess runs items concurrently with Python's event
  loop, so any baseline taken at ItemStartedNotification time races the
  command's own execution."""
  # Simulate: baseline captured BEFORE tool → file appears in after only.
  before = gf.Snapshot(
    files={"existing.txt": (1000000000, 50)},
    complete=True,
  )
  after = gf.Snapshot(
    files={
      "existing.txt": (1000000000, 50),
      "output.pdf": (1001000000, 1024),
    },
    complete=True,
  )
  diff = gf.diff_new_or_changed(before, after, own_chat_id="my-chat-id")
  assert len(diff) == 1
  assert diff[0]["path"] == "output.pdf"

  # Simulate: baseline captured AFTER tool (the bug) → file appears in
  # both before and after with the same fingerprint → diff is empty → no chip.
  before_after_tool = gf.Snapshot(
    files={
      "existing.txt": (1000000000, 50),
      "output.pdf": (1001000000, 1024),  # already present in "before"!
    },
    complete=True,
  )
  diff_after = gf.diff_new_or_changed(
    before_after_tool, after, own_chat_id="my-chat-id",
  )
  assert diff_after == [], (
    "baseline taken after tool produces empty diff — this is the bug "
    "that was introduced when baseline capture moved into "
    "acquire_cwd_reservation_hook / ItemStartedNotification (both race "
    "the tool) and was fixed by restoring capture to keepalive_hook / "
    "turn-start"
  )


def test_diff_publishes_nothing_from_an_incomplete_snapshot():
  """Fail closed: a partial view invents deliverables as readily as it misses
  them, and a wrongly recorded row is downloadable."""
  before = gf.Snapshot(files={}, complete=False)
  after = gf.Snapshot(files={"report.pdf": (123, 10)}, complete=True)
  assert gf.diff_new_or_changed(before, after, own_chat_id="c1") == []
  assert gf.diff_new_or_changed(after, before, own_chat_id="c1") == []


def test_snapshot_skips_other_chats_trees_and_symlinks(tmp_path):
  mine = tmp_path / "chats" / "mine"
  theirs = tmp_path / "chats" / "theirs"
  mine.mkdir(parents=True)
  theirs.mkdir(parents=True)
  (mine / "ok.pdf").write_bytes(b"%PDF-1.4")
  (theirs / "secret.pdf").write_bytes(b"%PDF-1.4")
  (tmp_path / "root.pdf").write_bytes(b"%PDF-1.4")

  snap = gf.snapshot(str(tmp_path), own_chat_id="mine")
  assert "root.pdf" in snap.files
  assert "chats/mine/ok.pdf" in snap.files
  assert "chats/theirs/secret.pdf" not in snap.files


def test_snapshot_only_records_allowlisted_extensions(tmp_path):
  (tmp_path / "keep.pdf").write_bytes(b"%PDF-1.4")
  (tmp_path / "skip.log").write_text("noise")
  (tmp_path / "skip.py").write_text("print()")
  snap = gf.snapshot(str(tmp_path), own_chat_id="c1")
  assert set(snap.files) == {"keep.pdf"}


def test_missing_cwd_is_incomplete_not_an_exception(tmp_path):
  snap = gf.snapshot(str(tmp_path / "does-not-exist"), own_chat_id="c1")
  assert snap.complete is True
  assert snap.files == {}


# --- read-only tools must not pay for detection ---------------------------


def test_read_only_tools_are_recognized_across_both_naming_shapes():
  assert gf.is_read_only_tool("WebSearch") is True
  assert gf.is_read_only_tool("mcp__mobius__web_search") is True
  assert gf.is_read_only_tool("mobius:web_fetch") is True
  assert gf.is_read_only_tool("Bash") is False
  assert gf.is_read_only_tool("mcp__mobius__write_report") is False
  assert gf.is_read_only_tool(None) is False


# --- download route: filename encoding ------------------------------------


def test_non_ascii_filename_downloads_instead_of_500(client, db, auth, chat):
  """Regression: a bare `filename="…"` is latin-1 on the wire, so a name the
  agent chose in the owner's own language raised UnicodeEncodeError inside the
  ASGI layer and 500'd the download."""
  settings = get_settings()
  (Path(settings.data_dir) / "report.pdf").write_bytes(b"%PDF-1.4 cjk")
  _write_row(db, chat, name="报告.pdf", path="report.pdf")

  token = client.post(
    f"/api/chats/{chat.id}/media-token", headers=auth,
  ).json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/报告.pdf", params={"token": token},
  )
  assert res.status_code == 200
  assert res.content == b"%PDF-1.4 cjk"
  disposition = res.headers["content-disposition"]
  assert "filename*=UTF-8''" in disposition
  # The ASCII fallback must still be a valid quoted-string for old clients.
  assert disposition.startswith('attachment; filename="')


def test_download_filename_cannot_break_out_of_the_quoted_string(db, chat):
  from app.path_utils import attachment_disposition

  built = attachment_disposition('evil".pdf')
  # Exactly the two delimiting quotes remain: the name's own quote was
  # sanitized away rather than terminating the quoted-string early.
  assert built.count('"') == 2
  assert 'filename="evil_.pdf"' in built
