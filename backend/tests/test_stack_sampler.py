"""The in-process sampler that stands in for py-spy inside the container."""

import threading
from types import SimpleNamespace

from app import stack_sampler


def _spin_in_named_hot_loop(stop: list[bool]) -> None:
  # Poll a plain flag, not Event.is_set(): a Python-level call inside the loop
  # would sometimes be the innermost frame the sampler records last.
  while not stop[0]:
    sum(range(200))


def test_busy_thread_ranks_first_and_parked_thread_counts_as_waiting():
  spinning = [False]
  parked_until = threading.Event()
  busy = threading.Thread(target=_spin_in_named_hot_loop, args=(spinning,), name="busy-worker")
  parked = threading.Thread(target=parked_until.wait, name="parked-worker")
  busy.start()
  parked.start()
  try:
    report = stack_sampler.sample_thread_stacks(0.5, interval=0.005)
  finally:
    spinning[0] = True
    parked_until.set()
    busy.join()
    parked.join()

  threads = {t["name"]: t for t in report["threads"]}
  assert threads["busy-worker"]["busy_percent"] > 50
  assert "_spin_in_named_hot_loop" in threads["busy-worker"]["last_busy_frame"]
  assert threads["parked-worker"]["busy_percent"] == 0
  assert threading.current_thread().name not in threads  # never itself


def test_known_idle_frames_count_as_waiting():
  """An idle uvloop thread's innermost frame is asyncio's runner; ranking it
  busy buried every real hot spot."""
  def frame(filename, name):
    return SimpleNamespace(f_code=SimpleNamespace(co_filename=filename, co_name=name))

  assert stack_sampler._is_waiting(frame("/usr/lib/python3.12/asyncio/runners.py", "run"))
  assert stack_sampler._is_waiting(frame("/x/openai_codex/client.py", "_read_message"))
  assert not stack_sampler._is_waiting(frame("/data/platform/backend/app/chat.py", "run_chat"))


def test_profile_endpoint_is_owner_only_bounded_and_single_flight(client, auth):
  assert client.get("/api/debug/profile?seconds=1").status_code == 401
  assert client.get("/api/debug/profile?seconds=61", headers=auth).status_code == 422
  response = client.get("/api/debug/profile?seconds=1", headers=auth)
  assert response.status_code == 200
  assert response.json()["samples"] >= 1

  assert stack_sampler._profile_lock.acquire(blocking=False)
  try:
    assert client.get("/api/debug/profile?seconds=1", headers=auth).status_code == 409
  finally:
    stack_sampler._profile_lock.release()
