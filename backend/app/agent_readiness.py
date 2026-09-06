"""Public, code-only readiness for starting agent work safely.

``GET /api/ready`` answers whether the service can serve chats: boot completed
and the single-writer persistence actor can accept commands. Agent work needs
additional platform-owned prerequisites such as writable storage and enough
resource headroom. This module composes those existing observations without
exposing operator details through the unauthenticated readiness route.

Provider connection state is advisory. Connected CLI providers have a cheap,
non-secret preflight, but the app-owned subscription depends on authenticated
owner state that a public machine probe must not query. Deployment readiness
therefore reflects whether the platform can safely accept provider work, not
whether one particular account or chat is configured to start it.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from app.resource_pressure import resource_status


CODE_BOOT_DEGRADED = "boot_degraded"
CODE_DATABASE_DEGRADED = "database_degraded"
CODE_WRITER_UNAVAILABLE = "writer_unavailable"
CODE_DATA_DISK_CRITICAL = "data_disk_critical"
CODE_DATA_DISK_CONSTRAINED = "data_disk_constrained"
CODE_DATA_DIR_UNWRITABLE = "data_dir_unwritable"
CODE_MEMORY_CRITICAL = "memory_critical"

CODE_DATA_DISK_UNKNOWN = "data_disk_unknown"
CODE_MEMORY_CONSTRAINED = "memory_constrained"
CODE_NO_AUTHENTICATED_PROVIDER = "no_authenticated_provider"
CODE_PROVIDER_UNAVAILABLE = "provider_unavailable"
CODE_PROTECTED_RUNTIME_STALE = "protected_runtime_stale"
CODE_PROTECTED_RUNTIME_UNAVAILABLE = "protected_runtime_unavailable"


# This route is public and may be polled frequently. Coalesce concurrent write
# canaries and briefly reuse the result so an unauthenticated caller cannot
# force one fsync per request.
_WRITABLE_PROBE_TTL_SECONDS = 15.0
_WRITABLE_PROBE_LOCK = threading.Lock()
_WRITABLE_PROBE_CACHE: dict[str, tuple[float, str | None]] = {}


def _default_writer_readiness() -> tuple[bool, str | None]:
  from app.chat_writer import writer_readiness

  return writer_readiness()


def _default_provider_report(data_dir: str) -> dict[str, str | None]:
  from app.providers import provider_auth_report

  return provider_auth_report(data_dir)


def _default_provenance(data_dir: str | Path) -> dict[str, Any]:
  from app.runtime_provenance import protected_runtime_status

  source_root = Path(data_dir) / "platform" / "backend" / "runtime"
  return dict(protected_runtime_status(source_root))


def _default_writable_probe(data_dir: str | Path) -> str | None:
  """Write, fsync, and remove a small canary under ``{data_dir}/run``."""
  run_dir = Path(data_dir) / "run"
  fd = None
  tmp_path = None
  error = None
  try:
    run_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".agent-readiness-", dir=run_dir)
    if os.write(fd, b"ok") != 2:
      error = "short write"
    os.fsync(fd)
  except OSError as exc:
    error = f"{type(exc).__name__}: {exc.strerror or 'write failed'}"
  finally:
    if fd is not None:
      try:
        os.close(fd)
      except OSError as exc:
        if error is None:
          error = f"{type(exc).__name__}: {exc.strerror or 'close failed'}"
    if tmp_path is not None:
      try:
        os.unlink(tmp_path)
      except OSError as exc:
        if error is None:
          error = f"{type(exc).__name__}: {exc.strerror or 'cleanup failed'}"
  return error


def _cached_writable_probe(
  data_dir: str | Path,
  *,
  probe: Callable[[str | Path], str | None] = _default_writable_probe,
  now: Callable[[], float] = time.monotonic,
) -> str | None:
  """Return one short-lived, concurrency-coalesced writable-volume proof."""
  key = str(Path(data_dir))
  with _WRITABLE_PROBE_LOCK:
    checked_at, result = _WRITABLE_PROBE_CACHE.get(
      key, (-float("inf"), None),
    )
    current = now()
    if current - checked_at < _WRITABLE_PROBE_TTL_SECONDS:
      return result
    result = probe(data_dir)
    _WRITABLE_PROBE_CACHE[key] = (current, result)
    return result


def _mapping(value: Any) -> dict[str, Any]:
  return value if isinstance(value, dict) else {}


def agent_readiness(
  data_dir: str | Path,
  *,
  boot_degraded: dict[str, Any] | None = None,
  status_reader: Callable[..., dict[str, Any]] | None = None,
  writer_readiness_reader: Callable[[], tuple[bool, str | None]] | None = None,
  provider_report_reader: Callable[[str], dict[str, str | None]] | None = None,
  provenance_reader: Callable[[str | Path], dict[str, Any]] | None = None,
  writable_probe: Callable[[str | Path], str | None] | None = None,
) -> dict[str, Any]:
  """Return the stable public readiness contract and no operator diagnostics."""
  status_reader = status_reader or resource_status
  writer_readiness_reader = (
    writer_readiness_reader or _default_writer_readiness
  )
  provider_report_reader = provider_report_reader or _default_provider_report
  provenance_reader = provenance_reader or _default_provenance
  writable_probe = writable_probe or _cached_writable_probe

  reasons: list[str] = []
  warnings: list[str] = []

  if boot_degraded:
    reasons.append(
      CODE_DATABASE_DEGRADED
      if boot_degraded.get("reason") in {
        "database_initialization_failed", "schema_mismatch",
      }
      else CODE_BOOT_DEGRADED
    )

  writer_ready, _writer_reason = writer_readiness_reader()
  if not writer_ready:
    reasons.append(CODE_WRITER_UNAVAILABLE)

  status = _mapping(status_reader(data_dir))
  pressure = _mapping(status.get("pressure"))
  disk_state = str(_mapping(pressure.get("disk")).get("state") or "unknown")
  if disk_state == "critical":
    reasons.append(CODE_DATA_DISK_CRITICAL)
  elif disk_state == "constrained":
    reasons.append(CODE_DATA_DISK_CONSTRAINED)
  elif disk_state != "normal":
    warnings.append(CODE_DATA_DISK_UNKNOWN)

  memory_state = str(
    _mapping(pressure.get("memory")).get("state") or "unknown"
  )
  if memory_state == "critical":
    reasons.append(CODE_MEMORY_CRITICAL)
  elif memory_state == "constrained":
    warnings.append(CODE_MEMORY_CONSTRAINED)

  if writable_probe(data_dir) is not None:
    reasons.append(CODE_DATA_DIR_UNWRITABLE)

  report = _mapping(provider_report_reader(str(data_dir)))
  authenticated = [
    provider for provider, error in report.items() if error is None
  ]
  if not authenticated:
    warnings.append(CODE_NO_AUTHENTICATED_PROVIDER)
  elif any(error is not None for error in report.values()):
    warnings.append(CODE_PROVIDER_UNAVAILABLE)

  provenance_state = _mapping(provenance_reader(data_dir)).get("state")
  if provenance_state == "stale":
    warnings.append(CODE_PROTECTED_RUNTIME_STALE)
  elif provenance_state == "unavailable":
    warnings.append(CODE_PROTECTED_RUNTIME_UNAVAILABLE)

  return {
    "ready": not reasons,
    "reason_code": reasons[0] if reasons else None,
    "reason_codes": reasons,
    "warning_codes": warnings,
  }
