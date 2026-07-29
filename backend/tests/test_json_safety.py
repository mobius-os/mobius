from pathlib import Path

from app.json_safety import json_safe


class ProviderPathWrapper:
  """Minimal stand-in for the Codex SDK's Pydantic root path wrapper."""

  def __init__(self, value):
    self.value = value

  def model_dump(self, **_kwargs):
    return self.value


def test_json_safe_normalizes_nested_provider_wrappers():
  assert json_safe({
    "cwd": ProviderPathWrapper("/data"),
    "paths": [Path("/data/platform"), ProviderPathWrapper("/tmp/app")],
  }) == {
    "cwd": "/data",
    "paths": ["/data/platform", "/tmp/app"],
  }
