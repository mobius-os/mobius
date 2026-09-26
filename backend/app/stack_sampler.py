"""In-process stack sampling for diagnosing a busy backend.

The app container normally lacks CAP_SYS_PTRACE, so external profilers
(py-spy, perf) cannot attach to the served process. Instead, one thread
periodically reads every other thread's current frame via
``sys._current_frames()`` and ranks where the busy ones were.

Threads parked in a known blocking wait count as waiting, so the ranking
points at code actually competing for the interpreter. Native code that
released the GIL shows at its Python call site. One profile runs at a time:
overlapping profiles would add overhead exactly when the process is
already saturated.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

# Innermost frames that mean "parked", as (file-name suffix, function name).
# An unlisted blocking call is reported as busy — visible rather than
# hidden — and belongs here once a real profile shows it.
_WAIT_FRAMES: frozenset[tuple[str, str]] = frozenset({
  ("threading.py", "wait"),
  ("threading.py", "_wait_for_tstate_lock"),
  ("queue.py", "get"),
  ("selectors.py", "select"),
  ("concurrent/futures/thread.py", "_worker"),
  # uvloop runs the loop natively, so an idle loop thread's innermost
  # Python frame is the runner; a busy loop shows its handler above it.
  ("asyncio/runners.py", "run"),
  # Codex's client blocks on line reads from its app-server's pipes.
  ("openai_codex/client.py", "_read_message"),
  ("openai_codex/client.py", "_drain"),
})

_APP_ROOT = str(Path(__file__).resolve().parent) + "/"
_STACK_DEPTH_LIMIT = 40
_INTERVAL_SECONDS = 0.02
_LIMIT = 20

_profile_lock = threading.Lock()


class ProfileInProgress(RuntimeError):
  """Another profile is already sampling this process."""


def _frame_label(frame) -> str:
  code = frame.f_code
  filename = code.co_filename
  if filename.startswith(_APP_ROOT):
    filename = "app/" + filename[len(_APP_ROOT):]
  else:
    # The last two path components identify a library module.
    filename = "/".join(filename.rsplit("/", 2)[-2:])
  return f"{code.co_qualname} ({filename}:{frame.f_lineno})"


def _is_waiting(frame) -> bool:
  code = frame.f_code
  return any(
    code.co_name == name and code.co_filename.endswith(suffix)
    for suffix, name in _WAIT_FRAMES
  )


def sample_thread_stacks(seconds: float, *, interval: float = _INTERVAL_SECONDS) -> dict:
  """Sample every other thread for ``seconds`` and rank where they were busy.

  Raises ``ProfileInProgress`` when another profile is running.
  """
  if not _profile_lock.acquire(blocking=False):
    raise ProfileInProgress("a profile is already running")
  try:
    return _sample(seconds, interval)
  finally:
    _profile_lock.release()


def _sample(seconds: float, interval: float) -> dict:
  own_id = threading.get_ident()
  app_frames: Counter[str] = Counter()
  stacks: Counter[tuple[str, ...]] = Counter()
  stack_threads: dict[tuple[str, ...], set[str]] = {}
  threads: dict[int, dict] = {}
  samples = busy = 0
  started_cpu = time.thread_time()
  deadline = time.monotonic() + seconds
  while True:
    names = {t.ident: t.name for t in threading.enumerate()}
    for ident, frame in sys._current_frames().items():
      if ident == own_id:
        continue
      thread = threads.setdefault(ident, {
        "name": names.get(ident, f"thread-{ident}"),
        "samples": 0, "busy": 0, "last_busy_frame": None,
      })
      thread["samples"] += 1
      if _is_waiting(frame):
        continue
      stack = []
      while frame is not None and len(stack) < _STACK_DEPTH_LIMIT:
        stack.append(_frame_label(frame))
        frame = frame.f_back
      stack = tuple(reversed(stack))
      busy += 1
      thread["busy"] += 1
      thread["last_busy_frame"] = stack[-1]
      # Once per stack, so recursion cannot inflate an app frame.
      app_frames.update(label for label in set(stack) if " (app/" in label)
      stacks[stack] += 1
      stack_threads.setdefault(stack, set()).add(thread["name"])
    samples += 1
    # Frames pin their locals; do not hold other threads' frames while asleep.
    frame = None
    if time.monotonic() >= deadline:
      break
    time.sleep(interval)

  def percent(count: int) -> float:
    return round(100 * count / busy, 1)

  return {
    "seconds": seconds,
    "samples": samples,
    "busy_observations": busy,
    # The sampler's own cost is part of the evidence.
    "sampler_cpu_seconds": round(time.thread_time() - started_cpu, 3),
    "threads": sorted((
      {"name": t["name"], "busy_percent": round(100 * t["busy"] / t["samples"], 1),
       "last_busy_frame": t["last_busy_frame"]}
      for t in threads.values()
    ), key=lambda t: t["busy_percent"], reverse=True),
    "hot_app_frames": [
      {"frame": label, "percent_of_busy": percent(count)}
      for label, count in app_frames.most_common(_LIMIT)
    ],
    "hot_stacks": [
      {"percent_of_busy": percent(count), "threads": sorted(stack_threads[stack]),
       "stack": list(stack)}
      for stack, count in stacks.most_common(_LIMIT // 2)
    ],
  }
