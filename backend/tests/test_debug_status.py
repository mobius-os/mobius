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
  golden_path = Path(__file__).with_name("golden_debug_status.json")
  expected = json.loads(golden_path.read_text(encoding="utf-8"))
  assert r.json() == expected
