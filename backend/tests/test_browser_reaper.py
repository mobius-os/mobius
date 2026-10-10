"""Browsers left by turns that died with the server are closed, and chat
browsers are held to a memory budget, without touching a live turn's browser
unless the budget forces it."""
import asyncio
from pathlib import Path
import shutil

import pytest

from app import browser_processes, chat


class _Registry:
  def __init__(self, alive=()):
    self.alive = set(alive)
    self.generations = {}

  def is_alive(self, chat_id):
    return chat_id in self.alive

  def current_generation(self, chat_id):
    return self.generations.get(chat_id, 0)


@pytest.fixture
def reaper(monkeypatch):
  closed = []
  state = {"usage": {}, "registry": _Registry()}

  async def close(chat_id):
    closed.append(chat_id)
    state["usage"].pop(chat_id, None)
    return True

  monkeypatch.setattr(chat, "_close_browser_session", close)
  monkeypatch.setattr(chat, "registry", state["registry"])
  monkeypatch.setattr(
    chat.browser_processes, "browser_usage_by_chat",
    lambda **_: {owner: browser_processes.BrowserChatUsage(pss)
                for owner, pss in state["usage"].items()},
  )
  state["closed"] = closed
  return state


def _run(budget=None):
  return asyncio.run(chat.reap_unowned_browsers(memory_budget_bytes=budget))


def test_browser_of_a_turn_that_died_with_the_server_is_closed(reaper):
  reaper["usage"] = {"dead": 2_600_000_000, "live": 100}
  reaper["registry"].alive.add("live")
  result = _run()
  assert reaper["closed"] == ["dead"]
  assert result == {"orphans_closed": {"dead": 2_600_000_000}, "guard_closed": {}}


def test_unknown_pss_orphan_closes_without_evicting_known_live_browser(tmp_path, monkeypatch):
  def browser(pid, owner, pss):
    process = tmp_path / str(pid)
    process.mkdir()
    fields = ["S", "1", *("0" for _ in range(17)), str(pid)]
    (process / "stat").write_text(f"{pid} (browser) {' '.join(fields)}\n")
    (process / "exe").symlink_to("/opt/chrome")
    (process / "cmdline").write_bytes(b"/opt/chrome\0")
    (process / "environ").write_bytes(f"CHAT_ID={owner}\0".encode())
    (process / "smaps_rollup").write_text(f"Pss: {pss} kB\n")

  browser(100, "dead", 2_000)
  browser(200, "live", 1_200)
  original_read = Path.read_text
  def read(path, *args, **kwargs):
    if path == tmp_path / "100" / "smaps_rollup":
      raise PermissionError("PSS unavailable")
    return original_read(path, *args, **kwargs)
  monkeypatch.setattr(Path, "read_text", read)

  real_usage = browser_processes.browser_usage_by_chat
  assert real_usage(proc_root=tmp_path) == {
    "dead": browser_processes.BrowserChatUsage(None),
    "live": browser_processes.BrowserChatUsage(1_200 * 1024),
  }
  monkeypatch.setattr(browser_processes, "browser_usage_by_chat",
                      lambda: real_usage(proc_root=tmp_path))
  registry = _Registry(alive={"live"})
  monkeypatch.setattr(chat, "registry", registry)
  closed = []
  async def close(chat_id):
    scan = browser_processes.scan_browser_processes(chat_id=chat_id, proc_root=tmp_path)
    assert scan.complete and scan.processes
    closed.append(chat_id)
    for process in scan.processes:
      shutil.rmtree(tmp_path / str(process.pid))
    return True
  monkeypatch.setattr(chat, "_close_browser_session", close)

  result = _run(budget=1_500 * 1024)
  assert result == {"orphans_closed": {"dead": None}, "guard_closed": {}}
  assert closed == ["dead"]
  assert (tmp_path / "200").is_dir()


def test_measured_zero_pss_orphan_is_still_closed(reaper):
  reaper["usage"] = {"dead": 0}
  assert _run() == {"orphans_closed": {"dead": 0}, "guard_closed": {}}
  assert reaper["closed"] == ["dead"]


def test_turn_starting_while_reaper_waits_for_the_lock_keeps_its_browser(reaper):
  reaper["usage"] = {"starting": 100}

  async def scenario():
    lock = chat._browser_lifecycle_lock("starting")
    await lock.acquire()
    task = asyncio.create_task(chat.reap_unowned_browsers(memory_budget_bytes=None))
    await asyncio.sleep(0)
    reaper["registry"].alive.add("starting")
    lock.release()
    return await task

  assert asyncio.run(scenario()) == {"orphans_closed": {}, "guard_closed": {}}
  assert reaper["closed"] == []


def test_orphan_nomination_does_not_close_new_generation(reaper):
  reaper["usage"] = {"starting": 100}

  async def scenario():
    lock = chat._browser_lifecycle_lock("starting")
    await lock.acquire()
    task = asyncio.create_task(chat.reap_unowned_browsers(memory_budget_bytes=None))
    for _ in range(1000):
      if lock._waiters:
        break
      await asyncio.sleep(0.001)
    assert lock._waiters
    reaper["registry"].generations["starting"] = 1
    lock.release()
    return await task

  assert asyncio.run(scenario()) == {"orphans_closed": {}, "guard_closed": {}}
  assert reaper["closed"] == []


def test_memory_guard_closes_largest_live_browsers_until_under_budget(reaper):
  reaper["usage"] = {"small": 300, "huge": 2_000, "medium": 900}
  reaper["registry"].alive.update({"small", "huge", "medium"})
  result = _run(budget=1_500)
  assert reaper["closed"] == ["huge"]
  assert result["guard_closed"] == {"huge": 2_000}


def test_memory_guard_counts_orphans_already_closed(reaper):
  reaper["usage"] = {"dead": 5_000, "live": 1_000}
  reaper["registry"].alive.add("live")
  result = _run(budget=1_500)
  assert reaper["closed"] == ["dead"]
  assert result["guard_closed"] == {}


def test_failed_close_is_not_reported_as_freed(reaper, monkeypatch):
  reaper["usage"] = {"dead": 2_000}
  async def fail(chat_id):
    reaper["closed"].append(chat_id)
    return False
  monkeypatch.setattr(chat, "_close_browser_session", fail)
  assert _run() == {"orphans_closed": {}, "guard_closed": {}}
  assert reaper["usage"] == {"dead": 2_000}


def test_session_close_reports_failed_exact_process_cleanup(monkeypatch):
  scan = browser_processes.BrowserSessionScan(
    frozenset(), True, (browser_processes.ProcessIdentity(100, 10),),
  )
  monkeypatch.setattr(
    chat.browser_profiles, "browser_session_targets_for_chat", lambda *args: scan,
  )
  def fail(processes):
    raise browser_processes.SessionResetError("did not exit")
  monkeypatch.setattr(browser_processes, "terminate_processes", fail)
  assert asyncio.run(chat._close_browser_session("dead")) is False


def test_guard_remeasures_shrunken_browser_under_lock(reaper):
  reaper["usage"] = {"was_huge": 2_000, "other": 900}
  reaper["registry"].alive.update({"was_huge", "other"})

  async def scenario():
    lock = chat._browser_lifecycle_lock("was_huge")
    await lock.acquire()
    task = asyncio.create_task(chat.reap_unowned_browsers(memory_budget_bytes=1_500))
    await asyncio.sleep(0.05)
    reaper["usage"]["was_huge"] = 10
    lock.release()
    return await task

  assert asyncio.run(scenario()) == {"orphans_closed": {}, "guard_closed": {}}
  assert reaper["closed"] == []


def test_guard_reselects_current_largest_after_lock_wait(reaper):
  reaper["usage"] = {"a": 3_000, "b": 2_000}
  reaper["registry"].alive.update({"a", "b"})

  async def scenario():
    lock = chat._browser_lifecycle_lock("a")
    await lock.acquire()
    task = asyncio.create_task(chat.reap_unowned_browsers(memory_budget_bytes=2_500))
    for _ in range(1000):
      if lock._waiters:
        break
      await asyncio.sleep(0.001)
    assert lock._waiters
    reaper["usage"].update(a=10, b=3_000)
    lock.release()
    return await task

  assert asyncio.run(scenario())["guard_closed"] == {"b": 3_000}
  assert reaper["closed"] == ["b"]


def test_guard_does_not_close_successor_generation_nominated_before_lock(reaper):
  reaper["usage"] = {"a": 3_000}
  reaper["registry"].alive.add("a")

  async def scenario():
    lock = chat._browser_lifecycle_lock("a")
    await lock.acquire()
    task = asyncio.create_task(chat.reap_unowned_browsers(memory_budget_bytes=2_500))
    for _ in range(1000):
      if lock._waiters:
        break
      await asyncio.sleep(0.001)
    assert lock._waiters
    reaper["registry"].generations["a"] = 1
    lock.release()
    return await task

  assert asyncio.run(scenario()) == {"orphans_closed": {}, "guard_closed": {}}
  assert reaper["closed"] == []
