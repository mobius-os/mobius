import errno
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.schema_migrations as migrations
from app import models
from app.config import get_settings
from app.schema_migrations import _move_chat_media_out_of_generated


def _write_legacy(chat, messages) -> None:
  """The previous release's stored value: 0084 rewrites this legacy column."""
  from sqlalchemy.orm import object_session
  object_session(chat).execute(text("UPDATE chats SET messages = :m WHERE id = :id"),
                               {"m": json.dumps(messages), "id": chat.id})


def _legacy_of(chat):
  from sqlalchemy.orm import object_session
  object_session(chat).expire(chat)
  return json.loads(object_session(chat).execute(
    text("SELECT messages FROM chats WHERE id = :id"), {"id": chat.id}).scalar())


def _chat_root(chat_id: str) -> Path:
  return Path(get_settings().data_dir) / "chats" / chat_id


def test_moves_files_and_rewrites_urls(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/old.png"
  new_url = f"/api/chats/{chat.id}/media/old.png"
  _write_legacy(chat, [{"role": "assistant", "content": f"![image]({old_url})"}])
  chat.pending_messages = [{"content": {"preview": old_url}}]
  db.commit()

  old_dir = _chat_root(chat.id) / "generated"
  old_dir.mkdir(parents=True)
  (old_dir / "old.png").write_bytes(b"old-image")

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert not old_dir.exists()
  assert (_chat_root(chat.id) / "media" / "old.png").read_bytes() == b"old-image"
  assert _legacy_of(chat)[0]["content"] == f"![image]({new_url})"
  assert chat.pending_messages[0]["content"]["preview"] == new_url


def test_symlinks_in_the_legacy_folder_are_never_copied(db, chat, tmp_path):
  outside = tmp_path / "outside-secret.txt"
  outside.write_bytes(b"secret")
  _write_legacy(chat, [{"role": "assistant", "content": (
    f"/api/chats/{chat.id}/generated/real.png "
    f"/api/chats/{chat.id}/generated/link.png"
  )}])
  db.commit()
  old_dir = _chat_root(chat.id) / "generated"
  old_dir.mkdir(parents=True)
  (old_dir / "real.png").write_bytes(b"real")
  (old_dir / "link.png").symlink_to(outside)

  _move_chat_media_out_of_generated(db.get_bind())

  media = _chat_root(chat.id) / "media"
  assert (media / "real.png").read_bytes() == b"real"
  assert not (media / "link.png").exists()
  assert outside.read_bytes() == b"secret"


def test_a_symlinked_legacy_folder_is_not_followed(db, chat, tmp_path):
  outside = tmp_path / "outside"
  outside.mkdir()
  (outside / "secret.png").write_bytes(b"secret")
  _write_legacy(chat, [{"role": "assistant", "content": (
    f"/api/chats/{chat.id}/generated/secret.png"
  )}])
  db.commit()
  root = _chat_root(chat.id)
  root.mkdir(parents=True, exist_ok=True)
  (root / "generated").symlink_to(outside, target_is_directory=True)

  _move_chat_media_out_of_generated(db.get_bind())

  assert not (root / "media" / "secret.png").exists()
  assert (outside / "secret.png").read_bytes() == b"secret"


def test_leaves_links_to_other_chats_untouched(db, chat):
  foreign = "/api/chats/someone-else/generated/example.png"
  _write_legacy(chat, [{"role": "assistant", "content": f"Discussing {foreign}"}])
  db.commit()

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert _legacy_of(chat)[0]["content"] == f"Discussing {foreign}"
  assert not (_chat_root(chat.id) / "media").exists()


def test_rewrites_legacy_url_without_old_directory(db, chat):
  old_url = f"/api/chats/{chat.id}/generated/already-moved.png"
  new_url = f"/api/chats/{chat.id}/media/already-moved.png"
  _write_legacy(chat, [{"role": "assistant", "content": old_url}])
  db.commit()

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert _legacy_of(chat)[0]["content"] == new_url


def test_retry_after_interrupted_cleanup_finishes_the_move(db, chat):
  """A crash after the link commit leaves both copies; a retry settles it."""
  new_url = f"/api/chats/{chat.id}/media/old.png"
  _write_legacy(chat, [{"role": "assistant", "content": new_url}])
  db.commit()
  for name in ("generated", "media"):
    directory = _chat_root(chat.id) / name
    directory.mkdir(parents=True)
    (directory / "old.png").write_bytes(b"old-image")

  _move_chat_media_out_of_generated(db.get_bind())
  db.refresh(chat)

  assert not (_chat_root(chat.id) / "generated").exists()
  assert (_chat_root(chat.id) / "media" / "old.png").read_bytes() == b"old-image"
  assert _legacy_of(chat)[0]["content"] == new_url


def _legacy_chat(session, data_dir: Path, image: bytes, media: bytes | None):
  """Add a chat linking ``generated/img.png``; optionally pre-seed ``media/``."""
  chat_id = str(uuid.uuid4())
  session.add(models.Chat(
    id=chat_id,
    title="Legacy",
    legacy_messages=[{
      "role": "assistant",
      "content": f"/api/chats/{chat_id}/generated/img.png",
    }],
  ))
  session.commit()
  chat_root = data_dir / "chats" / chat_id
  (chat_root / "generated").mkdir(parents=True)
  (chat_root / "generated" / "img.png").write_bytes(image)
  if media is not None:
    (chat_root / "media").mkdir(parents=True)
    (chat_root / "media" / "img.png").write_bytes(media)
  return chat_id


def test_collision_leaves_that_chat_as_is_and_migrates_the_others(
  db, caplog,
):
  data_dir = Path(get_settings().data_dir)
  colliding = _legacy_chat(db, data_dir, b"old", media=b"different")
  clean = _legacy_chat(db, data_dir, b"clean", media=None)

  with caplog.at_level("WARNING", logger="app.schema_migrations"):
    _move_chat_media_out_of_generated(db.get_bind())
  db.expire_all()

  stuck = db.get(models.Chat, colliding)
  assert _legacy_of(stuck)[0]["content"] == (
    f"/api/chats/{colliding}/generated/img.png"
  )
  assert (_chat_root(colliding) / "generated" / "img.png").read_bytes() == b"old"
  assert (_chat_root(colliding) / "media" / "img.png").read_bytes() == (
    b"different"
  )
  warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
  assert len(warnings) == 1
  assert colliding in warnings[0] and "img.png" in warnings[0]

  moved = db.get(models.Chat, clean)
  assert _legacy_of(moved)[0]["content"] == f"/api/chats/{clean}/media/img.png"
  assert not (_chat_root(clean) / "generated").exists()
  assert (_chat_root(clean) / "media" / "img.png").read_bytes() == b"clean"


def test_collision_does_not_block_database_startup(tmp_path, monkeypatch):
  """A media name collision must never stop the platform from booting."""
  data_dir = tmp_path / "data"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'boot.db'}")
  models.Base.metadata.create_all(eng)
  migrations._ensure_migration_ledger(eng)
  for version, _migration in migrations._SCHEMA_MIGRATIONS:
    if version != "0084_chat_media_directory":
      migrations._record_migration(eng, version)
  with Session(eng) as session:
    colliding = _legacy_chat(session, data_dir, b"old", media=b"different")
    clean = _legacy_chat(session, data_dir, b"clean", media=None)

  migrations.run_migrations(eng)

  assert "0084_chat_media_directory" in {
    row["version"] for row in migrations.schema_migration_history(eng)
  }
  with Session(eng) as session:
    assert _legacy_of(session.get(models.Chat, colliding))[0]["content"] == (
      f"/api/chats/{colliding}/generated/img.png"
    )
    assert _legacy_of(session.get(models.Chat, clean))[0]["content"] == (
      f"/api/chats/{clean}/media/img.png"
    )
  assert (data_dir / "chats" / colliding / "generated" / "img.png").exists()


def test_unreadable_legacy_file_does_not_block_database_startup(
  tmp_path, monkeypatch, caplog,
):
  """A file error leaves that chat as-is; the others move and 0084 records."""
  data_dir = tmp_path / "data"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'boot.db'}")
  models.Base.metadata.create_all(eng)
  migrations._ensure_migration_ledger(eng)
  for version, _migration in migrations._SCHEMA_MIGRATIONS:
    if version != "0084_chat_media_directory":
      migrations._record_migration(eng, version)
  with Session(eng) as session:
    unreadable = _legacy_chat(session, data_dir, b"locked", media=None)
    clean = _legacy_chat(session, data_dir, b"clean", media=None)
  real_copy = shutil.copy2

  def copy2(source, destination, **kwargs):
    if unreadable in str(source):
      raise PermissionError(errno.EACCES, "Permission denied", str(source))
    return real_copy(source, destination, **kwargs)

  monkeypatch.setattr(shutil, "copy2", copy2)

  with caplog.at_level("WARNING", logger="app.schema_migrations"):
    migrations.run_migrations(eng)

  assert "0084_chat_media_directory" in {
    row["version"] for row in migrations.schema_migration_history(eng)
  }
  with Session(eng) as session:
    assert _legacy_of(session.get(models.Chat, unreadable))[0]["content"] == (
      f"/api/chats/{unreadable}/generated/img.png"
    )
    assert _legacy_of(session.get(models.Chat, clean))[0]["content"] == (
      f"/api/chats/{clean}/media/img.png"
    )
  stuck_root = data_dir / "chats" / unreadable
  assert (stuck_root / "generated" / "img.png").read_bytes() == b"locked"
  assert not (stuck_root / "media" / "img.png").exists()
  assert (data_dir / "chats" / clean / "media" / "img.png").read_bytes() == (
    b"clean"
  )
  assert any(
    unreadable in r.getMessage()
    for r in caplog.records if r.levelname == "WARNING"
  )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_unenterable_chat_folder_does_not_block_database_startup(
  tmp_path, monkeypatch, caplog,
):
  """A chat folder that can be listed but not entered leaves only that chat."""
  data_dir = tmp_path / "data"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'boot.db'}")
  models.Base.metadata.create_all(eng)
  migrations._ensure_migration_ledger(eng)
  for version, _migration in migrations._SCHEMA_MIGRATIONS:
    if version != "0084_chat_media_directory":
      migrations._record_migration(eng, version)
  with Session(eng) as session:
    locked = _legacy_chat(session, data_dir, b"locked", media=None)
    clean = _legacy_chat(session, data_dir, b"clean", media=None)
  locked_root = data_dir / "chats" / locked
  locked_root.chmod(0o644)
  try:
    with caplog.at_level("WARNING", logger="app.schema_migrations"):
      migrations.run_migrations(eng)
  finally:
    locked_root.chmod(0o755)

  assert "0084_chat_media_directory" in {
    row["version"] for row in migrations.schema_migration_history(eng)
  }
  with Session(eng) as session:
    assert _legacy_of(session.get(models.Chat, locked))[0]["content"] == (
      f"/api/chats/{locked}/generated/img.png"
    )
    assert _legacy_of(session.get(models.Chat, clean))[0]["content"] == (
      f"/api/chats/{clean}/media/img.png"
    )
  assert (locked_root / "generated" / "img.png").read_bytes() == b"locked"
  assert any(
    locked in r.getMessage()
    for r in caplog.records if r.levelname == "WARNING"
  )


def test_rewrite_bumps_updated_at_only_for_rewritten_chats(db):
  """Browsers reuse a cached chat while updated_at matches, so a rewrite
  must advance it; a chat whose links were already current keeps its stamp."""
  data_dir = Path(get_settings().data_dir)
  legacy = _legacy_chat(db, data_dir, b"img", media=None)
  current = str(uuid.uuid4())
  db.add(models.Chat(
    id=current,
    title="Current",
    legacy_messages=[{"content": f"/api/chats/{current}/media/img.png"}],
  ))
  db.commit()
  # Leftover generated/ copy puts the current chat on the work list too.
  for name in ("generated", "media"):
    (_chat_root(current) / name).mkdir(parents=True)
    (_chat_root(current) / name / "img.png").write_bytes(b"img")
  stale = datetime(2020, 1, 1)
  with db.get_bind().begin() as conn:
    conn.execute(text("UPDATE chats SET updated_at = :stale"), {"stale": stale})

  _move_chat_media_out_of_generated(db.get_bind())
  db.expire_all()

  moved = db.get(models.Chat, legacy)
  assert _legacy_of(moved)[0]["content"] == f"/api/chats/{legacy}/media/img.png"
  assert moved.updated_at.replace(tzinfo=None) > stale
  untouched = db.get(models.Chat, current)
  assert untouched.updated_at.replace(tzinfo=None) == stale
  assert not (_chat_root(current) / "generated").exists()


def test_interrupted_copy_is_not_a_collision_on_retry(db, monkeypatch):
  """A copy cut short leaves no truncated media file for a retry to trip on."""
  data_dir = Path(get_settings().data_dir)
  chat_id = _legacy_chat(db, data_dir, b"full-image-bytes", media=None)

  def interrupted_copy(source, destination, **kwargs):
    Path(destination).write_bytes(b"full")
    raise OSError(errno.ENOSPC, "No space left on device", str(destination))

  with monkeypatch.context() as patch:
    patch.setattr(shutil, "copy2", interrupted_copy)
    _move_chat_media_out_of_generated(db.get_bind())
  assert not (_chat_root(chat_id) / "media" / "img.png").exists()

  _move_chat_media_out_of_generated(db.get_bind())
  db.expire_all()

  assert _legacy_of(db.get(models.Chat, chat_id))[0]["content"] == (
    f"/api/chats/{chat_id}/media/img.png"
  )
  assert (_chat_root(chat_id) / "media" / "img.png").read_bytes() == (
    b"full-image-bytes"
  )
  assert not (_chat_root(chat_id) / "generated").exists()


def test_upgrade_runs_the_move_once_and_later_boots_skip_the_scan(
  tmp_path, monkeypatch,
):
  """An instance that never ran the move is fixed once, then never rescanned."""
  data_dir = tmp_path / "data"
  monkeypatch.setenv("DATA_DIR", str(data_dir))
  eng = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
  models.Base.metadata.create_all(eng)
  migrations._ensure_migration_ledger(eng)
  for version, _migration in migrations._SCHEMA_MIGRATIONS:
    if version != "0084_chat_media_directory":
      migrations._record_migration(eng, version)

  chat_id = str(uuid.uuid4())
  old_url = f"/api/chats/{chat_id}/generated/old.png"
  with Session(eng) as session:
    session.add(models.Chat(
      id=chat_id,
      title="Legacy",
      legacy_messages=[{"role": "assistant", "content": old_url}],
    ))
    session.commit()
  old_dir = data_dir / "chats" / chat_id / "generated"
  old_dir.mkdir(parents=True)
  (old_dir / "old.png").write_bytes(b"old-image")

  migrations.run_migrations(eng)

  def stored_content() -> str:
    with Session(eng) as session:
      return _legacy_of(session.get(models.Chat, chat_id))[0]["content"]

  assert stored_content() == f"/api/chats/{chat_id}/media/old.png"
  assert (data_dir / "chats" / chat_id / "media" / "old.png").exists()
  assert not old_dir.exists()
  assert "0084_chat_media_directory" in {
    row["version"] for row in migrations.schema_migration_history(eng)
  }

  # A legacy link that appears after completion stays as written: the
  # recorded migration is not replayed, so no boot scans transcripts again.
  with eng.begin() as conn:
    conn.execute(text(
      "UPDATE chats SET messages = :messages WHERE id = :chat_id"
    ), {"messages": f'[{{"content": "{old_url}"}}]', "chat_id": chat_id})
  migrations.run_migrations(eng)
  assert stored_content() == old_url
