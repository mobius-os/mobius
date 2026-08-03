from app.chat import (
  DEFAULT_VIEWPORT_HEIGHT,
  DEFAULT_VIEWPORT_PIXEL_RATIO,
  DEFAULT_VIEWPORT_WIDTH,
  bounded_agent_browser_args,
  viewport_env,
)
from app.providers import ClaudeProvider, CodexProvider


def test_codex_build_env_sets_agent_browser_session(tmp_path):
  env = CodexProvider().build_env(
    base_env={
      "AGENT_BROWSER_PROFILE": "/profiles/chat-1",
      "AGENT_BROWSER_ARGS": (
        "--disk-cache-size=33554432,--media-cache-size=16777216"
      ),
    },
    data_dir=str(tmp_path),
    chat_id="abc-123",
  )

  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "codex")
  assert env["AGENT_BROWSER_PROFILE"] == "/profiles/chat-1"
  assert env["AGENT_BROWSER_SESSION"] == "chat-abc-123"
  assert env["AGENT_BROWSER_ARGS"] == (
    "--disk-cache-size=33554432,--media-cache-size=16777216"
  )


def test_codex_build_env_without_chat_id_does_not_invent_session(tmp_path):
  env = CodexProvider().build_env(
    base_env={},
    data_dir=str(tmp_path),
    chat_id=None,
  )

  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "codex")
  assert "AGENT_BROWSER_SESSION" not in env


def test_codex_build_env_exposes_connected_claude_for_reverse_delegation(
  tmp_path,
):
  claude_dir = tmp_path / "cli-auth" / "claude"
  claude_dir.mkdir(parents=True)
  (claude_dir / ".credentials.json").write_text("{}")

  env = CodexProvider().build_env(
    base_env={},
    data_dir=str(tmp_path),
    chat_id="delegating-chat",
  )

  assert env["CLAUDE_CONFIG_DIR"] == str(claude_dir)


def test_codex_build_env_does_not_advertise_unconnected_claude(tmp_path):
  env = CodexProvider().build_env(
    base_env={},
    data_dir=str(tmp_path),
    chat_id="codex-only",
  )

  assert "CLAUDE_CONFIG_DIR" not in env


def test_claude_and_codex_use_same_agent_browser_session_name(tmp_path):
  claude_env = ClaudeProvider().build_env(
    base_env={},
    data_dir=str(tmp_path),
    chat_id="same-chat",
  )
  codex_env = CodexProvider().build_env(
    base_env={},
    data_dir=str(tmp_path),
    chat_id="same-chat",
  )

  assert claude_env["AGENT_BROWSER_SESSION"] == "chat-same-chat"
  assert codex_env["AGENT_BROWSER_SESSION"] == "chat-same-chat"


def test_browser_cache_defaults_preserve_operator_flags_and_overrides():
  assert bounded_agent_browser_args("--no-sandbox,--disk-cache-size=123") == (
    "--no-sandbox,--disk-cache-size=123,--media-cache-size=16777216"
  )


# CSS geometry and physical density form one agent-browser capture contract:
# chat.py validates it once per turn and agent-screenshot.sh repeats that
# validation for existing sessions and manual callers.


def test_viewport_env_passes_through_the_shell_sent_viewport():
  env = viewport_env({"width": 390, "height": 844, "pixelRatio": 3})
  assert env == {
    "VIEWPORT_WIDTH": "390",
    "VIEWPORT_HEIGHT": "844",
    "VIEWPORT_PIXEL_RATIO": "3",
  }


def test_viewport_env_rounds_fractional_shell_geometry_to_css_pixels():
  env = viewport_env({
    "width": 1680,
    "height": 956.6666870117188,
    "pixelRatio": 2.625,
  })
  assert env == {
    "VIEWPORT_WIDTH": "1680",
    "VIEWPORT_HEIGHT": "957",
    "VIEWPORT_PIXEL_RATIO": "2.625",
  }


def test_viewport_env_defaults_when_no_shell_sent_a_viewport():
  # Shell-less turns (cron, reflection, background continuations from
  # apps.py / platform_update.py) never send a viewport; the documented
  # default keeps screenshots working there instead of hard-failing.
  env = viewport_env(None)
  assert env == {
    "VIEWPORT_WIDTH": str(DEFAULT_VIEWPORT_WIDTH),
    "VIEWPORT_HEIGHT": str(DEFAULT_VIEWPORT_HEIGHT),
    "VIEWPORT_PIXEL_RATIO": f"{DEFAULT_VIEWPORT_PIXEL_RATIO:g}",
  }


def test_viewport_env_defaults_on_malformed_viewport():
  # A half-set or zero payload must not export a broken pair — the
  # helper requires BOTH values, so anything short of that defaults.
  for bad in (
    {},
    {"width": 390},
    {"height": 844},
    {"width": 0, "height": 915},
    {"width": float("nan"), "height": 915},
  ):
    env = viewport_env(bad)
    assert env["VIEWPORT_WIDTH"] == str(DEFAULT_VIEWPORT_WIDTH)
    assert env["VIEWPORT_HEIGHT"] == str(DEFAULT_VIEWPORT_HEIGHT)
    assert env["VIEWPORT_PIXEL_RATIO"] == f"{DEFAULT_VIEWPORT_PIXEL_RATIO:g}"


def test_viewport_env_bounds_untrusted_pixel_density_without_losing_geometry():
  assert viewport_env({
    "width": 390, "height": 844, "pixelRatio": 99,
  }) == {
    "VIEWPORT_WIDTH": "390",
    "VIEWPORT_HEIGHT": "844",
    "VIEWPORT_PIXEL_RATIO": "4",
  }
  assert viewport_env({
    "width": 390, "height": 844, "pixelRatio": "not-a-number",
  }) == {
    "VIEWPORT_WIDTH": "390",
    "VIEWPORT_HEIGHT": "844",
    "VIEWPORT_PIXEL_RATIO": "1",
  }
