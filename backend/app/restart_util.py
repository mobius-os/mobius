"""Reliable in-process worker restart for the owner-facing restart paths.

Shared by ``/api/admin/restart`` (the Settings "Restart" button) and
``/api/platform/restart`` (the platform-update "Restart to finish" button) so
the two can never drift apart — a restart that works in one place but hangs in
the other is exactly the bug this consolidates away.
"""

from __future__ import annotations

import os
import signal
import threading

# How long to let uvicorn try a graceful shutdown before we hard-kill. Short:
# the response is already flushed (this runs as a BackgroundTask), and uvicorn's
# graceful shutdown otherwise waits on open connections (the chat SSE stream
# never closes on its own) with no timeout of its own.
_FORCE_KILL_AFTER_SECONDS = 5.0


def restart_this_worker() -> None:
  """Restart this uvicorn worker so it reboots with the current code.

  SIGTERM asks uvicorn to shut down gracefully — but uvicorn's graceful
  shutdown blocks on open connections, and the chat SSE stream never closes on
  its own. With no graceful-shutdown timeout a plain SIGTERM therefore hangs the
  process in shutdown limbo: it stops serving but never exits, so tini (PID 1)
  never exits and the container never restarts. That is the "pressed Restart and
  the server never came back" outage.

  So we ARM a hard-kill fallback first, then send SIGTERM. If uvicorn drains and
  exits within the window, the process is gone and the daemon timer dies with it
  (SIGKILL never fires). If it hangs, the timer force-exits the process, the
  container cycles, and a fresh worker boots. Data is safe: the chat writer
  commits before any response returns, so a graceful drain would not have saved
  anything a hard kill loses.
  """
  pid = os.getpid()

  def _force_exit() -> None:
    os.kill(pid, signal.SIGKILL)

  timer = threading.Timer(_FORCE_KILL_AFTER_SECONDS, _force_exit)
  timer.daemon = True
  timer.start()
  os.kill(pid, signal.SIGTERM)
