"""Shared helper hosts: many delegated helper turns in one provider process.

A delegated helper keeps its durable Möbius identity — a hidden child chat
with its own transcript, run history, result, and parent wake-up (see
``delegations.py``). This module changes only where that child's turn
executes. Instead of starting a fresh provider process per helper turn, every
helper turn of one parent chat and one setup runs inside one long-lived
provider process (a *host*):

- Codex (and Möbius models, which run on the Codex harness): one app-server;
  each helper is a thread, resumed for its turn with that turn's settings and
  unloaded afterwards.
- Claude: one Claude Code process; each helper is a built-in background agent
  that Möbius launches with exact settings (see ``claude_helper_host``).

A helper in a host costs roughly what the provider's own built-in helper costs
(measured: ~5–14 MB each instead of ~135–260 MB per process).

Per-turn identity. A host's process environment is shared by every helper in
it, so nothing run- or helper-specific may live there. Each helper turn gets:

- non-secret values (chat id, run marker, scratch dir, …) passed per turn, and
- secrets (the helper's access token, …) in a private per-turn env file that
  its shell commands and tool servers load, deleted when the turn ends. Only
  the file's path is ever handed to the provider, which may persist settings in
  its session history.

Hosts are keyed by (parent chat, provider, access scope, working directory,
connected-services plan), so one chat's crash, secrets, or permissions never
reach another chat's helpers. A host shuts down after it has been idle for
``HOST_IDLE_SECONDS``; a dead host is replaced on next use.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

log = logging.getLogger(__name__)

HOST_IDLE_SECONDS = 90.0
# A host holds the connected-services capabilities minted for its first turn
# (valid 24 h); an idle host older than this is replaced so they never lapse.
HOST_MAX_AGE_SECONDS = 12 * 60 * 60
# Worker threads per Codex host: each concurrently running helper turn holds
# one while waiting on its thread's notifications.
CODEX_HOST_WORKERS = 64
# Marks every host process so a host orphaned by a server crash can be found
# and ended at the next boot. Its value names the owning server incarnation and
# the host key digest (non-secret): see host_marker.
HOST_MARKER_ENV = "MOBIUS_HELPER_HOST"
# Env entries that describe the run rather than the host. They are passed per
# helper turn, never baked into the shared host process.
PER_TURN_ENV = frozenset({
  "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN", "MOBIUS_RUN_MARKER",
  "MOBIUS_SUBAGENT_DEPTH", "MOBIUS_DELEGATION_ID", "MOBIUS_SUBAGENT_PROVIDER",
  "MOBIUS_SUBAGENT_HELPER", "MOBIUS_COORDINATION_ENABLED",
  "TMPDIR", "TMP", "TEMP",
  "AGENT_BROWSER_PROFILE", "AGENT_BROWSER_SESSION", "AGENT_BROWSER_NAMESPACE",
  "AGENT_BROWSER_SOCKET_DIR",
})
# Per-turn entries safe to hand to a provider verbatim (it may persist them).
PUBLIC_TURN_ENV = frozenset({
  "CHAT_ID", "MOBIUS_RUN_MARKER", "MOBIUS_SUBAGENT_DEPTH",
  "MOBIUS_DELEGATION_ID", "MOBIUS_SUBAGENT_PROVIDER", "MOBIUS_SUBAGENT_HELPER",
  "MOBIUS_COORDINATION_ENABLED", "TMPDIR", "TMP", "TEMP",
})
# The control tool server loads a helper turn's identity from this file.
CALLER_ENV_FILE_ENV = "MOBIUS_CALLER_ENV_FILE"


def hosts_enabled() -> bool:
  """Operational kill switch: MOBIUS_HELPER_HOSTS=0 runs helpers per process."""
  return os.environ.get("MOBIUS_HELPER_HOSTS", "1").strip().lower() not in (
    "0", "off", "false", "no",
  )


def is_per_turn_env(name: str) -> bool:
  return name in PER_TURN_ENV or name.startswith("VIEWPORT_") or name.startswith(
    "MOBIUS_APP_",
  )


def split_env(env: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
  """``(host_env, turn_env)``: what the shared process may hold vs per turn."""
  host: dict[str, str] = {}
  turn: dict[str, str] = {}
  for name, value in env.items():
    (turn if is_per_turn_env(name) else host)[name] = value
  return host, turn


@dataclass(frozen=True)
class HostKey:
  parent_chat_id: str
  provider_id: str
  scope: str
  cwd: str
  setup: str  # digest of the host-level configuration (connectors, overrides)

  @property
  def digest(self) -> str:
    raw = json.dumps(
      [self.parent_chat_id, self.provider_id, self.scope, self.cwd, self.setup],
    ).encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def setup_digest(*parts: Any) -> str:
  return hashlib.sha256(
    json.dumps(parts, sort_keys=True, default=str).encode(),
  ).hexdigest()[:24]


class TurnEnvFile:
  """A helper turn's secret environment in a private file, removed at exit.

  Shell commands load it through ``BASH_ENV``; tool servers through
  ``MOBIUS_CALLER_ENV_FILE``. The file lives in the helper's own scratch
  directory with owner-only permissions.
  """

  def __init__(self, directory: Path, marker: str, values: dict[str, str]):
    directory.mkdir(parents=True, exist_ok=True)
    self.path = directory / f".helper-turn-{marker or 'run'}.env"
    lines = [
      f"export {name}={shlex.quote(value)}"
      for name, value in sorted(values.items())
      if name.isidentifier()
    ]
    fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
      handle.write("\n".join(lines) + "\n")
    os.chmod(self.path, 0o600)

  def remove(self) -> None:
    with contextlib.suppress(FileNotFoundError):
      self.path.unlink()


def load_env_file(path: str) -> dict[str, str]:
  """Parse a TurnEnvFile (``export NAME=quoted``) without running a shell."""
  values: dict[str, str] = {}
  try:
    text = Path(path).read_text()
  except OSError:
    return values
  for line in text.splitlines():
    line = line.strip()
    if not line.startswith("export "):
      continue
    name, sep, raw = line[len("export "):].partition("=")
    if not sep or not name.isidentifier():
      continue
    parsed = shlex.split(raw) if raw else [""]
    values[name] = parsed[0] if parsed else ""
  return values


class Host:
  """Common lease/idle lifecycle; subclasses own the provider process."""

  def __init__(self, key: HostKey):
    self.key = key
    self._leases = 0
    self._idle_task: asyncio.Task | None = None
    self._closed = False
    self._born = time.monotonic()

  @property
  def expired(self) -> bool:
    return (
      self._leases == 0
      and time.monotonic() - self._born > HOST_MAX_AGE_SECONDS
    )

  @property
  def alive(self) -> bool:
    return not self._closed

  async def start(self) -> None:  # pragma: no cover - provider specific
    raise NotImplementedError

  async def close(self) -> None:  # pragma: no cover - provider specific
    self._closed = True

  def _acquire(self) -> None:
    self._leases += 1
    if self._idle_task is not None:
      self._idle_task.cancel()
      self._idle_task = None

  def _release(self, manager: "HostManager") -> None:
    self._leases = max(0, self._leases - 1)
    if self._leases == 0 and not self._closed:
      self._idle_task = asyncio.create_task(manager._close_when_idle(self))


class HostManager:
  """Owns every live host in this server process."""

  def __init__(self) -> None:
    self._hosts: dict[HostKey, Host] = {}
    self._locks: dict[HostKey, asyncio.Lock] = {}

  def _lock(self, key: HostKey) -> asyncio.Lock:
    lock = self._locks.get(key)
    if lock is None:
      lock = self._locks[key] = asyncio.Lock()
    return lock

  @contextlib.asynccontextmanager
  async def lease(
    self, key: HostKey, factory: Callable[[], Host],
  ) -> AsyncIterator[Host]:
    """Hold one live host for the duration of a helper turn."""
    async with self._lock(key):
      host = self._hosts.get(key)
      if host is None or not host.alive or host.expired:
        if host is not None:
          with contextlib.suppress(Exception):
            await host.close()
        await self._close_idle_siblings(key)
        host = factory()
        await host.start()
        self._hosts[key] = host
        log.info("helper host started key=%s provider=%s", key.digest, key.provider_id)
      host._acquire()
    try:
      yield host
    finally:
      host._release(self)

  async def _close_idle_siblings(self, key: HostKey) -> None:
    """A parent's setup changed: release its old idle hosts for the provider.

    A helper's conversation can be open in one host at a time (Codex refuses a
    second writer), so an old host that still holds it must go before a new
    host for the same parent and provider resumes that helper.
    """
    for other_key, other in list(self._hosts.items()):
      if (
        other_key != key
        and other_key.parent_chat_id == key.parent_chat_id
        and other_key.provider_id == key.provider_id
        and other._leases == 0
      ):
        del self._hosts[other_key]
        if other._idle_task is not None:
          other._idle_task.cancel()
        with contextlib.suppress(Exception):
          await other.close()

  async def discard(self, host: Host) -> None:
    """Replace a broken host on next use."""
    async with self._lock(host.key):
      if self._hosts.get(host.key) is host:
        del self._hosts[host.key]
    with contextlib.suppress(Exception):
      await host.close()

  async def _close_when_idle(self, host: Host) -> None:
    try:
      await asyncio.sleep(HOST_IDLE_SECONDS)
    except asyncio.CancelledError:
      return
    async with self._lock(host.key):
      if host._leases or self._hosts.get(host.key) is not host:
        return
      del self._hosts[host.key]
    log.info("helper host idle; closing key=%s", host.key.digest)
    with contextlib.suppress(Exception):
      await host.close()

  async def close_all(self) -> None:
    hosts = list(self._hosts.values())
    self._hosts.clear()
    for host in hosts:
      with contextlib.suppress(Exception):
        await host.close()

  def live_hosts(self) -> list[HostKey]:
    return [key for key, host in self._hosts.items() if host.alive]


MANAGER = HostManager()


def host_marker(digest: str) -> str:
  """Marker value for a host this server starts: ``<pid>:<start ticks>:<digest>``.

  Naming the owning server incarnation lets boot cleanup end only hosts whose
  server is gone, never hosts a still-running server owns (an overlapping
  server during replacement, or a test run on a live instance).
  """
  from app.process_groups import _start_ticks
  own = os.getpid()
  return f"{own}:{_start_ticks(own)}:{digest}"


def _owner_alive(marker: bytes) -> bool:
  from app.process_groups import _start_ticks
  try:
    pid, ticks, _digest = marker.split(b":", 2)
    return _start_ticks(int(pid)) == int(ticks)
  except (OSError, ProcessLookupError, ValueError, IndexError):
    return False


def end_orphaned_hosts() -> int:
  """At boot: end host processes whose owning server is no longer running."""
  from app.process_groups import _signal_same_process, _start_ticks
  ended = 0
  needle = f"{HOST_MARKER_ENV}=".encode()
  own = os.getpid()
  for entry in os.listdir("/proc"):
    if not entry.isdigit() or int(entry) == own:
      continue
    pid = int(entry)
    try:
      with open(f"/proc/{pid}/environ", "rb") as handle:
        marker = next(
          (v[len(needle):] for v in handle.read().split(b"\0") if v.startswith(needle)),
          None,
        )
      if marker is None or _owner_alive(marker):
        continue
      ticks = _start_ticks(pid)
    except (OSError, ProcessLookupError, ValueError, IndexError):
      continue
    if _signal_same_process(pid, ticks, signal.SIGKILL):
      ended += 1
  if ended:
    log.info("ended %d orphaned helper-host process(es)", ended)
  return ended


# --------------------------------------------------------------------- Codex


class CodexHelperHost(Host):
  """One Codex app-server shared by one parent's helper threads."""

  def __init__(self, key: HostKey, *, sdk: dict, config: Any):
    super().__init__(key)
    self._sdk = sdk
    self._config = config
    self._context: Any = None
    self.client: Any = None
    self.process_group_id: int | None = None
    self._executor: Any = None

  @property
  def alive(self) -> bool:
    if self._closed or self.client is None:
      return False
    proc = getattr(getattr(getattr(self.client, "_client", None), "_sync", None), "_proc", None)
    return proc is None or proc.poll() is None

  async def start(self) -> None:
    from app.codex_sdk_runner import (
      _codex_process_group_id,
      _enter_codex_context_owned,
      _install_codex_call_executor,
      _install_delegated_approval_handler,
    )
    from app.process_groups import lower_process_group_priority
    self._context = self._sdk["AsyncCodex"](config=self._config)
    # Every live helper turn holds one worker while it waits for its thread's
    # notifications, so a host needs far more than a single turn's three.
    self._executor = _install_codex_call_executor(
      self._context, f"helper-host:{self.key.digest}", CODEX_HOST_WORKERS,
    )
    client, cancel = await _enter_codex_context_owned(self._context)
    self.client = client
    if cancel is not None:
      await self.close()
      raise cancel
    self.process_group_id = _codex_process_group_id(client)
    lower_process_group_priority(
      self.process_group_id, logger=log, label="Codex helper host",
    )
    # Helpers deny provider-side escalation; the same fail-closed handler
    # serves every thread because it never depends on which one asked.
    _install_delegated_approval_handler(client, chat_id=self.key.parent_chat_id)

  async def unload_thread(self, thread_id: str | None) -> None:
    """Unload one helper thread after its turn; its tool servers exit."""
    if not thread_id or self.client is None:
      return
    from openai_codex.generated.v2_all import ThreadUnsubscribeResponse
    try:
      await self.client._client.request(
        "thread/unsubscribe", {"threadId": thread_id},
        response_model=ThreadUnsubscribeResponse,
      )
    except Exception:
      log.debug("helper thread unload failed thread=%s", thread_id, exc_info=True)

  async def close(self) -> None:
    self._closed = True
    context, self._context = self._context, None
    if context is not None:
      with contextlib.suppress(Exception):
        await context.__aexit__(None, None, None)
    if self.process_group_id is not None:
      from app.process_groups import terminate_process_group
      await asyncio.to_thread(
        terminate_process_group, self.process_group_id,
        logger=log, label="Codex helper host",
      )
      self.process_group_id = None
    if self._executor is not None:
      with contextlib.suppress(Exception):
        self._executor.close()
      self._executor = None


def codex_turn_thread_config(
  base: dict[str, Any] | None,
  *,
  turn_env: dict[str, str],
  env_file: TurnEnvFile,
) -> dict[str, Any]:
  """Thread settings that carry one helper turn's identity.

  Non-secret per-turn values go straight into the shell environment; secrets
  reach commands through ``BASH_ENV`` (Codex runs commands with ``bash -lc``)
  and the Möbius control tool server through its caller env file.
  """
  from app.platform_tools import CONTROL_SERVER_NAME
  config = json.loads(json.dumps(base or {}))
  public = {
    name: value for name, value in turn_env.items() if name in PUBLIC_TURN_ENV
  }
  public["BASH_ENV"] = str(env_file.path)
  config.setdefault("shell_environment_policy", {}).setdefault("set", {}).update(public)
  servers = config.get("mcp_servers") or {}
  control = servers.get(CONTROL_SERVER_NAME)
  if isinstance(control, dict):
    control["env_vars"] = [
      name for name in control.get("env_vars", [])
      if not is_per_turn_env(name)
    ]
    control["env"] = {
      CALLER_ENV_FILE_ENV: str(env_file.path),
      "MOBIUS_RUN_MARKER": turn_env.get("MOBIUS_RUN_MARKER", ""),
    }
  return config
