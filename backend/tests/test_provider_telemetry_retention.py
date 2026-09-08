"""Tests for the provider-telemetry retention DRY-RUN planner (#5).

The planner is pure — it deletes nothing and never reads the filesystem — so it
is fully exercised with synthetic entries. These lock the conservative policy:
current-generation DBs, provider auth/config, and recently-active files are
NEVER reclaimed; only superseded, stale, mechanically-safe classes are.
"""

from __future__ import annotations

from app import provider_telemetry_retention as ret
from app.provider_telemetry_retention import Entry, plan_reclamation

NOW = 1_000_000.0
OLD = NOW - 500_000  # well past the 1-day active window
RECENT = NOW - 10


def _names(items):
  return {item["name"] for item in items}


def test_plan_preserves_auth_config_and_current_generation():
  entries = [
    Entry("state_5.sqlite", 500, RECENT),          # current gen -> preserve
    Entry("state_3.sqlite", 100, OLD),             # superseded + stale -> reclaim
    Entry("state_4.sqlite", 200, RECENT),          # superseded but recent -> preserve
    Entry("auth.json", 10, OLD),                   # auth -> preserve
    Entry("config.toml", 20, OLD),                 # config -> preserve
    Entry(".credentials.json", 30, OLD),           # credentials -> preserve
  ]
  plan = plan_reclamation(entries, now=NOW)
  assert _names(plan["reclaim"]) == {"state_3.sqlite"}
  assert plan["live_generation"] == {"state": 5}
  preserved = _names(plan["preserve"])
  assert {"state_5.sqlite", "state_4.sqlite", "auth.json",
          "config.toml", ".credentials.json"} <= preserved
  assert plan["reclaimable_bytes"] == 100


def test_plan_reclaims_ephemeral_and_rotated_only_when_stale():
  entries = [
    Entry("cache", 4096, OLD, is_dir=True),        # stale scratch -> reclaim
    Entry("tmp", 4096, RECENT, is_dir=True),       # recent scratch -> preserve
    Entry("codex-login.log.1", 40, OLD),           # rotated log -> reclaim
    Entry("codex-login.log", 50, OLD),             # CURRENT log -> preserve
  ]
  plan = plan_reclamation(entries, now=NOW)
  assert _names(plan["reclaim"]) == {"cache", "codex-login.log.1"}
  assert {"tmp", "codex-login.log"} <= _names(plan["preserve"])


def test_plan_keeps_newest_claude_json_backups():
  entries = [
    Entry(".claude.json", 60, RECENT),                 # the live file -> preserve
    Entry(".claude.json.100", 10, NOW - 100),          # newest backups kept...
    Entry(".claude.json.200", 10, NOW - 200),
    Entry(".claude.json.300", 10, NOW - 300),
    Entry(".claude.json.900", 10, OLD),                # ...oldest, stale -> reclaim
  ]
  plan = plan_reclamation(entries, now=NOW, keep_json_backups=3)
  assert _names(plan["reclaim"]) == {".claude.json.900"}
  assert ".claude.json" in _names(plan["preserve"])


def test_unclassified_entries_are_preserved_by_default():
  entries = [Entry("something_unexpected.dat", 999, OLD)]
  plan = plan_reclamation(entries, now=NOW)
  assert plan["reclaim"] == []
  assert _names(plan["preserve"]) == {"something_unexpected.dat"}


def test_retention_is_disabled_by_default(monkeypatch):
  monkeypatch.delenv("MOBIUS_TELEMETRY_RETENTION_ENABLED", raising=False)
  assert ret.retention_enabled() is False
  monkeypatch.setenv("MOBIUS_TELEMETRY_RETENTION_ENABLED", "1")
  assert ret.retention_enabled() is True
  # The module exposes NO deletion function — reclamation cannot be triggered
  # from here even when the gate is on.
  assert not hasattr(ret, "apply_reclamation")
  assert not hasattr(ret, "reclaim")
