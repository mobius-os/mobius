"""Bounded capacity observation ring + forecast + alert-storm suppression.

Outcome #7: a full ``/data`` should be seen COMING, not just when admission
already refuses turns. That needs three things the platform lacked — a persisted
observation history (every capacity read was a point sample lost at restart), a
forecast (rate-of-change / time-to-exhaustion), and de-duplicated alerting so a
threshold cross does not storm the owner.

Persistence is a capped JSONL ring under ``{data_dir}/logs`` with high-water
trim — cheap, restart-durable, no schema migration. Writes are best-effort
(swallow ``OSError``) and strictly capped, so the monitor can never amplify the
very pressure it watches by growing an unbounded history during a disk-full
event. ``oom_diagnostics`` reuses this same ring idiom for OOM events.

Alert suppression is a pure tier-transition rule: notify only when the tier
ESCALATES (cross-down to a worse tier); the 8/5/2 GiB tier bands give inherent
hysteresis (recovering from ``critical`` to ``warning`` requires climbing back
above 2 GiB, and re-alerting critical requires dropping below 2 GiB again), so a
value hovering at a boundary never storms.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.capacity import TIER_RANK, domain_label

_HISTORY_HIGH_WATER = 2200
_HISTORY_TRIM_TARGET = 2000

_MIN_FORECAST_SAMPLES = 3
_MIN_FORECAST_SPAN_S = 300.0


def _history_path(data_dir: str | Path) -> Path:
  return Path(data_dir) / "logs" / "capacity-history.jsonl"


def _alert_state_path(data_dir: str | Path) -> Path:
  return Path(data_dir) / "logs" / "capacity-alert-state.json"


def record_sample(data_dir: str | Path, sample: dict[str, Any]) -> bool:
  """Append one capacity sample to the capped ring. Best-effort.

  ``sample`` should carry at least ``ts`` (epoch seconds) and
  ``data_free_bytes``. Returns True when the append succeeded, False on any
  OSError (the monitor keeps running regardless).
  """
  path = _history_path(data_dir)
  line = json.dumps(sample, separators=(",", ":"), default=str)
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
      f.write(line + "\n")
  except OSError:
    return False
  # Amortized trim: only rewrite when we cross the high-water mark.
  try:
    if _line_count(path) > _HISTORY_HIGH_WATER:
      _trim(path)
  except OSError:
    pass
  return True


def _line_count(path: Path) -> int:
  with open(path, encoding="utf-8") as f:
    return sum(1 for _ in f)


def _trim(path: Path) -> None:
  with open(path, encoding="utf-8") as f:
    retained = deque(f, maxlen=_HISTORY_TRIM_TARGET)
  staging = path.with_name(f".{path.name}.tmp")
  with open(staging, "w", encoding="utf-8") as f:
    f.writelines(retained)
  os.replace(staging, path)


def load_samples(data_dir: str | Path, *, limit: int | None = None) -> list[dict]:
  path = _history_path(data_dir)
  samples: list[dict] = []
  try:
    with open(path, encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          samples.append(json.loads(line))
        except ValueError:
          continue
  except OSError:
    return []
  return samples[-limit:] if limit else samples


def forecast(
  samples: list[dict],
  *,
  min_samples: int = _MIN_FORECAST_SAMPLES,
  min_span_s: float = _MIN_FORECAST_SPAN_S,
) -> dict[str, Any]:
  """Least-squares free-bytes trend + time-to-exhaustion over the window.

  Returns ``{"status": "insufficient_history", ...}`` until there are enough
  observations spanning a minimum interval — never a fabricated rate from one
  or two points.
  """
  pts = [
    (float(s["ts"]), float(s["data_free_bytes"]))
    for s in samples
    if isinstance(s.get("ts"), (int, float))
    and isinstance(s.get("data_free_bytes"), (int, float))
  ]
  if len(pts) < min_samples:
    return {"status": "insufficient_history", "samples_used": len(pts)}
  t0 = pts[0][0]
  xs = [t - t0 for t, _ in pts]
  ys = [f for _, f in pts]
  span = xs[-1] - xs[0]
  if span < min_span_s:
    return {
      "status": "insufficient_history",
      "samples_used": len(pts),
      "span_seconds": span,
    }
  n = len(pts)
  sx = sum(xs)
  sy = sum(ys)
  sxx = sum(x * x for x in xs)
  sxy = sum(x * y for x, y in zip(xs, ys))
  denom = n * sxx - sx * sx
  if denom == 0:
    return {"status": "insufficient_history", "samples_used": n}
  slope = (n * sxy - sx * sy) / denom  # bytes per second
  current_free = ys[-1]
  tte = (current_free / -slope) if slope < 0 else None
  return {
    "status": "ok",
    "rate_bytes_per_sec": slope,
    "time_to_exhaustion_seconds": tte,
    "samples_used": n,
    "span_seconds": span,
  }


def load_alert_state(data_dir: str | Path) -> dict[str, Any]:
  try:
    with open(_alert_state_path(data_dir), encoding="utf-8") as f:
      state = json.load(f)
      return state if isinstance(state, dict) else {}
  except (OSError, ValueError):
    return {}


def save_alert_state(data_dir: str | Path, state: dict[str, Any]) -> None:
  path = _alert_state_path(data_dir)
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.tmp")
    with open(staging, "w", encoding="utf-8") as f:
      json.dump(state, f, separators=(",", ":"), default=str)
    os.replace(staging, path)
  except OSError:
    pass


def evaluate_alert(current_tier: str | None, previous_tier: str | None) -> bool:
  """Notify only on ESCALATION to a worse tier (storm-suppressed, hysteretic).

  Equal tier => already alerted, stay quiet. Lower tier => recovery, quiet (the
  caller records the new baseline). The 8/5/2 GiB bands are the hysteresis: you
  cannot re-alert a tier without first recovering out of it.
  """
  current_rank = TIER_RANK.get(current_tier or "none", 0)
  previous_rank = TIER_RANK.get(previous_tier or "none", 0)
  return current_rank > previous_rank


def alert_notification_id(tier: str, *, day: str | None = None) -> str:
  """Deterministic per-tier-per-day id so retries within a day de-duplicate."""
  day = day or datetime.now(UTC).strftime("%Y%m%d")
  return f"capacity-{tier}-{day}"


# Hosts quote volume sizes in decimal GB, and owners compare this alert with
# them, so its math uses the same decimal units its GB/MB labels name.
_GB = 1000 ** 3
_MB = 1000 ** 2


def _size(value: int) -> str:
  return f"{value / _GB:.1f} GB" if value >= _GB else f"{value // _MB} MB"


def alert_body(snapshot: dict[str, Any], forecast_result: dict[str, Any] | None) -> str:
  """The owner-facing alert: how much is left, what happens next, what to do."""
  free = snapshot.get("data_free_bytes")
  total = snapshot.get("data_total_bytes")
  if isinstance(free, int) and isinstance(total, int) and total > 0:
    parts = [f"Möbius has {_size(free)} of {_size(total)} storage left."]
  elif isinstance(free, int):
    parts = [f"Möbius has {_size(free)} of storage left."]
  else:
    parts = ["Möbius storage is running low."]
  floor = ((snapshot.get("mounts") or {}).get("data") or {}).get("critical_below_bytes")
  if isinstance(floor, int) and floor > 0:
    parts.append(f"Chats pause automatically below {_size(floor)}.")
  domains = snapshot.get("domains") or {}
  sized = [
    (name, d.get("bytes", 0))
    for name, d in domains.items()
    if isinstance(d, dict) and isinstance(d.get("bytes"), int)
  ]
  incomplete = sorted(
    name for name, domain in domains.items()
    if isinstance(domain, dict) and domain.get("truncated") is True
  )
  if sized and not incomplete:
    name, size = max(sized, key=lambda kv: kv[1])
    parts.append(f"The largest use is {domain_label(name).lower()} ({_size(size)}).")
  elif incomplete:
    # Partial totals must not be used to name a largest owner.
    parts.append(
      "Sizes were only partly measured (scan-limited: "
      + ", ".join(domain_label(name).lower() for name in incomplete) + ")."
    )
  tte = (forecast_result or {}).get("time_to_exhaustion_seconds")
  if isinstance(tte, (int, float)) and tte > 0:
    parts.append(f"At the current rate it fills in about {tte / 3600:.1f}h.")
  parts.append("Open Storage and Memory in Settings to free up space, or grow the volume.")
  return " ".join(parts)


def sample_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
  """Project a full capacity_snapshot down to the compact history row."""
  mounts = snapshot.get("mounts") or {}
  host_root = mounts.get("host_root") or {}
  return {
    "ts": time.time(),
    "captured_at": snapshot.get("captured_at"),
    "data_free_bytes": snapshot.get("data_free_bytes"),
    "data_total_bytes": snapshot.get("data_total_bytes"),
    "host_root_free_bytes": host_root.get("free_bytes"),
    "alert_tier": snapshot.get("alert_tier"),
  }
