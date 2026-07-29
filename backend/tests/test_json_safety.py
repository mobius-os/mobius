from pathlib import Path

from app.json_safety import json_safe


def test_json_safe_normalizes_nested_provider_wrappers():
  from openai_codex.generated.v2_all import LegacyAppPathString

  assert json_safe({
    "cwd": LegacyAppPathString("/data"),
    "paths": [Path("/data/platform"), LegacyAppPathString("/tmp/app")],
  }) == {
    "cwd": "/data",
    "paths": ["/data/platform", "/tmp/app"],
  }
