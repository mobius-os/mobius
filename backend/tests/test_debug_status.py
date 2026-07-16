"""Golden-file coverage for `/api/debug/status`."""

import json
from pathlib import Path

from app.runner_registry import RunnerKind, registry


class _Handle:
  def __init__(self, chat_id: str, kind: RunnerKind):
    self.chat_id = chat_id
    self.kind = kind

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    return True


def test_debug_status_shape_matches_golden(client, auth):
  registry.register(_Handle("chat-sdk-claude", RunnerKind.CLAUDE_SDK))
  registry.register(_Handle("chat-sdk-codex", RunnerKind.CODEX_SDK))
  registry.mark_starting("chat-starting")

  r = client.get("/api/debug/status", headers=auth)

  assert r.status_code == 200
  payload = r.json()
  pool = payload.pop("database_pool")
  assert pool["type"]
  assert pool["current"]["checked_out"] >= 1
  assert pool["lifetime"]["checkouts"] >= 1
  assert pool["lifetime"]["long_checkouts"] >= 0
  assert pool["lifetime"]["max_checkout_ms"] >= 0
  watcher = payload.pop("frontend_watcher")
  assert watcher["status"] in {
    "starting", "waiting_for_lease", "running", "stopped", "unavailable",
  }
  assert isinstance(watcher["running"], bool)
  allocator = payload.pop("allocator")
  assert allocator["source"] in {
    "not_attempted", "environment", "mallopt", "mallopt_rejected",
    "unsupported", "environment_invalid",
  }
  assert isinstance(allocator["applied"], bool)
  browser_profiles = payload.pop("browser_profiles")
  assert browser_profiles["profile_count"] >= 0
  assert browser_profiles["reclaimed_bytes"] >= 0
  golden_path = Path(__file__).with_name("golden_debug_status.json")
  expected = json.loads(golden_path.read_text(encoding="utf-8"))
  assert payload == expected


def test_debug_status_surfaces_media_migration_failure(client, auth):
  client.app.state.media_migration_failed = True
  try:
    response = client.get("/api/debug/status", headers=auth)
  finally:
    client.app.state.media_migration_failed = False

  assert response.status_code == 200
  assert response.json()["media_migration_failed"] is True
