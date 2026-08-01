"""Agent scratch stays on the bounded data volume and does not outlive its run.

The container's /tmp is an overlay upperdir: no quota, statvfs reporting host
capacity, and not a tmpfs, so nothing ever clears it. Moving scratch to the
data volume bounds it — but that volume also holds SQLite, so scratch without
a lifecycle would take durable data down with it. These cover both halves.
"""

import time

from app import agent_scratch, config, models


def _pin_data_dir(monkeypatch, tmp_path):
  monkeypatch.setattr(
    config, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})()
  )
  return tmp_path


def test_scratch_root_follows_the_configured_volume(monkeypatch, tmp_path):
  """A hardcoded path would satisfy a single-volume assertion, so move the
  volume and require the root to move with it."""
  _pin_data_dir(monkeypatch, tmp_path / "volume-a")
  first = config.agent_scratch_root()
  _pin_data_dir(monkeypatch, tmp_path / "volume-b")
  second = config.agent_scratch_root()

  assert first != second
  assert (tmp_path / "volume-a") in first.parents
  assert (tmp_path / "volume-b") in second.parents


def test_scratch_for_chat_is_usable_before_the_subprocess_starts(
  monkeypatch, tmp_path
):
  """The path is exported straight into a subprocess environment, so it has to
  exist by then rather than on first write."""
  _pin_data_dir(monkeypatch, tmp_path)

  assert agent_scratch.scratch_for_chat("chat-1").is_dir()


def test_scratch_is_isolated_per_chat(monkeypatch, tmp_path):
  """Sweeping is per chat, so two chats must not share one directory."""
  _pin_data_dir(monkeypatch, tmp_path)

  assert agent_scratch.scratch_for_chat("a") != agent_scratch.scratch_for_chat("b")


def test_sweep_reclaims_scratch_for_a_chat_with_no_run_in_flight(
  monkeypatch, tmp_path, db
):
  _pin_data_dir(monkeypatch, tmp_path)
  idle = agent_scratch.scratch_for_chat("idle-chat")
  (idle / "leftover.bin").write_bytes(b"x" * 2048)

  result = agent_scratch.sweep_idle_scratch(
    db, now=time.time() + agent_scratch._SWEEP_GRACE_SECONDS + 1
  )

  assert not idle.exists()
  assert result["removed"] == 1
  assert result["bytes"] >= 2048


def test_sweep_never_deletes_scratch_of_a_run_still_in_flight(
  monkeypatch, tmp_path, db, chat
):
  """Deleting a live run's scratch would corrupt a turn already in progress."""
  _pin_data_dir(monkeypatch, tmp_path)
  chat_id = getattr(chat, "id", chat)
  db.add(
    models.ChatRun(
      id="run-live", chat_id=chat_id, status="running"
    )
  )
  db.commit()
  live = agent_scratch.scratch_for_chat(chat_id)

  result = agent_scratch.sweep_idle_scratch(
    db, now=time.time() + agent_scratch._SWEEP_GRACE_SECONDS + 1
  )

  assert live.is_dir()
  assert result["kept_live"] == 1
  assert result["removed"] == 0


def test_sweep_spares_scratch_created_before_its_run_row_exists(
  monkeypatch, tmp_path, db
):
  """Scratch and the ChatRun row are created around the same moment and are not
  ordered against each other, so a run starting now must not delete the scratch
  of one that has not registered yet."""
  _pin_data_dir(monkeypatch, tmp_path)
  starting = agent_scratch.scratch_for_chat("just-started")

  result = agent_scratch.sweep_idle_scratch(db)

  assert starting.is_dir()
  assert result["kept_recent"] == 1
  assert result["removed"] == 0


def test_sweep_reports_zero_before_any_scratch_exists(monkeypatch, tmp_path, db):
  """First run on a fresh volume: absence is an ordinary result, not an error."""
  _pin_data_dir(monkeypatch, tmp_path / "never-written")

  assert agent_scratch.sweep_idle_scratch(db)["removed"] == 0
