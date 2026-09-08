"""Check subprocess ownership lasts only from admission through reaping."""

import asyncio
import shlex

import pytest

from app import chat_waits


@pytest.fixture
def command_wait(client, owner_token, db):
  response = client.post(
    "/api/chats", json={"title": "Check processes"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200
  return chat_waits.declare_wait(
    db, chat_id=response.json()["id"], description="Process ownership",
    condition_owner="test executor", kind="command", command="sleep 30",
    deadline_secs=3600,
  )


def assert_released(wait_id):
  with chat_waits._ACTIVE_CHECKS_LOCK:
    assert wait_id not in chat_waits._ACTIVE_CHECK_PIDS
    assert wait_id not in chat_waits._CANCELLED_CHECK_IDS


def test_cancelling_inactive_checks_retains_no_process_state():
  for index in range(1000):
    wait_id = f"inactive-check-{index}"
    chat_waits._cancel_active_check(wait_id)
    assert_released(wait_id)


def test_cancelled_wait_is_not_admitted_without_an_in_memory_tombstone(
  command_wait, db, monkeypatch,
):
  chat_waits.cancel_wait(db, command_wait)
  assert_released(command_wait.id)

  async def unexpected_spawn(*args, **kwargs):
    pytest.fail("a durably cancelled check must not spawn")

  monkeypatch.setattr(asyncio, "create_subprocess_shell", unexpected_spawn)
  result = asyncio.run(chat_waits._run_check(
    command_wait.command, wait_id=command_wait.id,
  ))
  assert result == (-1, "check cancelled")
  assert_released(command_wait.id)


def test_cancellation_during_spawn_reaps_the_admitted_process(
  command_wait, db, monkeypatch,
):
  real_spawn = asyncio.create_subprocess_shell
  processes = []

  async def cancel_during_spawn(*args, **kwargs):
    chat_waits.cancel_wait(db, command_wait)
    process = await real_spawn(*args, **kwargs)
    processes.append(process)
    return process

  monkeypatch.setattr(asyncio, "create_subprocess_shell", cancel_during_spawn)
  result = asyncio.run(chat_waits._run_check(
    command_wait.command, wait_id=command_wait.id,
  ))
  assert result[0] != 0
  assert processes[0].returncode is not None
  assert_released(command_wait.id)


def test_failed_spawn_releases_check_admission(command_wait, monkeypatch):
  async def failed_spawn(*args, **kwargs):
    raise OSError("spawn failed")

  monkeypatch.setattr(asyncio, "create_subprocess_shell", failed_spawn)
  with pytest.raises(OSError, match="spawn failed"):
    asyncio.run(chat_waits._run_check(
      command_wait.command, wait_id=command_wait.id,
    ))
  assert_released(command_wait.id)


def test_supervisor_cancellation_during_spawn_still_reaps_process(
  command_wait, monkeypatch,
):
  real_spawn = asyncio.create_subprocess_shell
  processes = []

  async def exercise():
    spawned = asyncio.Event()
    release = asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
      process = await real_spawn(*args, **kwargs)
      processes.append(process)
      spawned.set()
      await release.wait()
      return process

    monkeypatch.setattr(asyncio, "create_subprocess_shell", delayed_spawn)
    task = asyncio.create_task(chat_waits._run_check(
      command_wait.command, wait_id=command_wait.id,
    ))
    await asyncio.wait_for(spawned.wait(), timeout=2)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
      await asyncio.wait_for(task, timeout=2)
    assert processes[0].returncode is not None
    assert_released(command_wait.id)

  asyncio.run(exercise())


def test_timeout_reaps_children_after_the_shell_has_exited(tmp_path, monkeypatch):
  pid_file = tmp_path / "child.pid"
  monkeypatch.setattr(chat_waits, "CHECK_TIMEOUT_SECS", 0.1)
  result = asyncio.run(chat_waits._run_check(
    f"sleep 30 & echo $! > {shlex.quote(str(pid_file))}; exit 0",
  ))
  assert result == (-1, "check timed out after 0.1s")
  child_pid = int(pid_file.read_text())
  # An adopted zombie may await the container's init; it must not be running.
  try:
    with open(f"/proc/{child_pid}/stat") as stat:
      assert stat.read().split()[2] == "Z"
  except FileNotFoundError:
    pass
