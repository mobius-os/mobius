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
  memory = payload.pop("memory")
  assert memory["process"]["available"] is True
  assert memory["process"]["rss_bytes"] > 0
  assert memory["tracing"]["source"]
  resources = payload.pop("resources")
  assert set(resources) == {"facts", "pressure"}
  assert resources["facts"]["disk"]["available"] is True
  assert resources["facts"]["memory"]["available"] is True
  assert resources["pressure"]["state"] in {
    "normal", "constrained", "critical", "unknown",
  }
  runtime_memory = payload.pop("runtime_memory")
  assert runtime_memory["runner_handles"]["claude_sdk"] == 1
  assert runtime_memory["runner_handles"]["codex_sdk"] == 1
  assert runtime_memory["starting_chats"] == 1
  assert isinstance(runtime_memory["broadcasts"], list)
  assert isinstance(runtime_memory["active_sinks"], list)
  assert "present" in runtime_memory["writer"]
  assert runtime_memory["questions"]["pending_count"] >= 0
  assert runtime_memory["payload_sizing"] == {
    "included": False,
    "omitted": True,
    "detail_url": "/api/debug/memory",
  }
  public_apps = payload.pop("public_apps")
  assert set(public_apps) == {"apps", "transport"}
  assert all(str(app_id).isdigit() for app_id in public_apps["apps"])
  assert set(public_apps["transport"]) == {
    "clients", "active_requests", "active_limit", "clients_created",
    "clients_evicted",
  }
  assert public_apps["transport"]["active_limit"] > 0
  assert all(value >= 0 for value in public_apps["transport"].values())
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


def test_debug_memory_report_is_authenticated_and_bounded(client, auth):
  assert client.get("/api/debug/memory").status_code == 401

  response = client.get(
    "/api/debug/memory?allocation_limit=3&process_limit=3",
    headers=auth,
  )

  assert response.status_code == 200
  payload = response.json()
  assert payload["process"]["available"] is True
  assert payload["process"]["rss_bytes"] > 0
  assert payload["memory_maps"]["available"] is True
  assert len(payload["processes"]["top_processes"]) <= 3
  assert payload["gc"]["counts"]
  assert "top_tracked_types" not in payload["gc"]
  assert "enabled" in payload["allocations"]
  assert "checkpoints" in payload
  assert payload["runtime_memory"]["payload_sizing"] == {"included": True}


def test_debug_status_does_not_silently_enable_guessed_payload_option(
  client, auth,
):
  response = client.get(
    "/api/debug/status?include_payloads=true",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.json()["runtime_memory"]["payload_sizing"] == {
    "included": False,
    "omitted": True,
    "detail_url": "/api/debug/memory",
  }


def test_debug_logs_bounds_each_returned_line(client, auth, tmp_path, monkeypatch):
  from app.routes import debug

  log_dir = tmp_path / "logs"
  log_dir.mkdir()
  (log_dir / "chat.log").write_text("start-" + ("x" * 5000) + "-finish\n")
  monkeypatch.setattr(
    debug,
    "get_settings",
    lambda: type("Settings", (), {"data_dir": str(tmp_path)})(),
  )

  response = client.get("/api/debug/logs?lines=10", headers=auth)

  assert response.status_code == 200
  line = response.json()["lines"][0]
  assert len(line) == 4000
  assert "characters truncated" in line
  assert line.endswith("-finish")
