"""Bounded capacity observation ring + forecast + alert-storm suppression.

Outcome #7: a full ``/data`` should be seen COMING, not just when admission
already refuses turns. That needs three things the platform lacked — a persisted
observation history (every capacity read was a point sample lost at restart), a
forecast (rate-of-change / time-to-exhaustion), and de-duplicated alerting so a
threshold cross does not storm the owner.

Persistence reuses ``routes/debug.py``'s perf-samples idiom: a capped JSONL ring
under ``{data_dir}/logs`` with high-water trim — cheap, restart-durable, no
schema migration. Writes are best-effort (swallow ``OSError`` like perf-samples)
and strictly capped, so the monitor can never amplify the very pressure it
watches by growing an unbounded history during a disk-full event.

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

from app.capacity import GIB, TIER_RANK

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


def alert_body(snapshot: dict[str, Any], forecast_result: dict[str, Any] | None) -> str:
  free = snapshot.get("data_free_bytes")
  tier = snapshot.get("alert_tier") or "notice"
  free_str = f"{free / GIB:.2f} GiB" if isinstance(free, int) else "an unknown amount"
  parts = [f"/data has {free_str} free (alert: {tier})."]
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
    parts.append(f"Largest domain: {name} ({size / GIB:.2f} GiB).")
  elif incomplete:
    parts.append(
      "Domain attribution was scan-limited; partial totals must not be used "
      "to name a largest owner. Incomplete: " + ", ".join(incomplete) + "."
    )
  tte = (forecast_result or {}).get("time_to_exhaustion_seconds")
  if isinstance(tte, (int, float)) and tte > 0:
    parts.append(f"~{tte / 3600:.1f}h to exhaustion at the current rate.")
  parts.append("Reclaim space or grow the volume.")
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
