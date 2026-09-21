"""Per-provider quota signal and quota-aware background provider selection.

Locks in that unattended/app-initiated provider selection walks the owner's
background-agents list to the first provider with usage quota, records a limit's
reset time, and heals when the window elapses or the provider runs again.
"""

import json
from datetime import timedelta

from app import background_agents as bg
from app import provider_availability as pa
from app.models import ProviderAvailability
from app.timeutil import now_naive_utc

BG_LIST = [
  {"provider": "claude", "model": "claude-opus-4-8", "effort": "medium", "enabled": True},
  {"provider": "codex", "model": "gpt-5.5", "effort": "medium", "enabled": True},
]


def _write_bg(tmp_path, providers_list):
  d = tmp_path / "shared"
  d.mkdir(parents=True, exist_ok=True)
  (d / "agent-settings.json").write_text(
    json.dumps({"background_agents": {"providers": providers_list}})
  )


def _connect_all(monkeypatch):
  """Treat every catalog provider as connected for the walk.

  Patch the provider CLASS, not the shared singleton instance: an instance
  attribute survives ``monkeypatch``'s revert as a shadow that would defeat a
  later test's class-level auth patch (e.g. the provider-switch suite reading
  Codex as "not signed in").
  """
  from app import providers
  for pid in ("claude", "codex", "mobius"):
    prov = providers.PROVIDERS.get(pid)
    if prov is not None:
      monkeypatch.setattr(type(prov), "check_auth", lambda self, data_dir: None)


# --- quota primitives ---------------------------------------------------------

def test_within_quota_true_when_no_row(db):
  assert pa.provider_within_quota(db, "claude") is True


def test_limit_blocks_until_reset_then_clear_heals(db):
  pa.mark_provider_limited(
    db, "claude", now_naive_utc() + timedelta(hours=1), "usage_limit",
  )
  db.commit()
  assert pa.provider_within_quota(db, "claude") is False
  pa.clear_provider_availability(db, "claude")
  db.commit()
  assert pa.provider_within_quota(db, "claude") is True


def test_limit_in_the_past_is_within_quota(db):
  pa.mark_provider_limited(
    db, "codex", now_naive_utc() - timedelta(minutes=1), "usage_limit",
  )
  db.commit()
  assert pa.provider_within_quota(db, "codex") is True


def test_mark_limited_keeps_the_latest_reset(db):
  # Two separate park events (each its own commit): a shorter later window must
  # not shorten the longer live limit.
  later = now_naive_utc() + timedelta(hours=2)
  pa.mark_provider_limited(db, "claude", later, "usage_limit")
  db.commit()
  pa.mark_provider_limited(
    db, "claude", now_naive_utc() + timedelta(minutes=5), "rate_limit",
  )
  db.commit()
  row = db.get(ProviderAvailability, "claude")
  assert row.limited_until == later


# --- resolve_background_provider ---------------------------------------------

def test_resolve_returns_first_within_quota(tmp_path, db, monkeypatch):
  _write_bg(tmp_path, BG_LIST)
  _connect_all(monkeypatch)
  assert bg.resolve_background_provider(str(tmp_path), db)["provider"] == "claude"


def test_resolve_walks_past_a_limited_primary(tmp_path, db, monkeypatch):
  _write_bg(tmp_path, BG_LIST)
  _connect_all(monkeypatch)
  pa.mark_provider_limited(
    db, "claude", now_naive_utc() + timedelta(hours=1), "usage_limit",
  )
  db.commit()
  assert bg.resolve_background_provider(str(tmp_path), db)["provider"] == "codex"


def test_resolve_falls_back_to_first_when_all_limited(tmp_path, db, monkeypatch):
  _write_bg(tmp_path, BG_LIST)
  _connect_all(monkeypatch)
  for pid in ("claude", "codex"):
    pa.mark_provider_limited(
      db, pid, now_naive_utc() + timedelta(hours=1), "usage_limit",
    )
  db.commit()
  # None have quota -> the owner's rule: use the first entry anyway.
  assert bg.resolve_background_provider(str(tmp_path), db)["provider"] == "claude"


def test_prefer_provider_keeps_current_while_within_quota(tmp_path, db, monkeypatch):
  _write_bg(tmp_path, BG_LIST)
  _connect_all(monkeypatch)
  choice = bg.resolve_background_provider(
    str(tmp_path), db, prefer_provider="codex",
  )
  assert choice["provider"] == "codex"


def test_prefer_provider_yields_when_that_provider_is_limited(tmp_path, db, monkeypatch):
  _write_bg(tmp_path, BG_LIST)
  _connect_all(monkeypatch)
  pa.mark_provider_limited(
    db, "codex", now_naive_utc() + timedelta(hours=1), "usage_limit",
  )
  db.commit()
  choice = bg.resolve_background_provider(
    str(tmp_path), db, prefer_provider="codex",
  )
  assert choice["provider"] == "claude"
