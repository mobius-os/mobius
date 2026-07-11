from app.chat import (
  DEFAULT_VIEWPORT_HEIGHT,
  DEFAULT_VIEWPORT_WIDTH,
  viewport_env,
)
from app.providers import ClaudeProvider, CodexProvider


def test_codex_build_env_sets_agent_browser_session(tmp_path):
  env = CodexProvider().build_env(
    base_env={"AGENT_BROWSER_PROFILE": "/profiles/chat-1"},
    data_dir=str(tmp_path),
    chat_id="abc-123",
  )

  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "codex")
  assert env["AGENT_BROWSER_PROFILE"] == "/profiles/chat-1"
  assert env["AGENT_BROWSER_SESSION"] == "chat-abc-123"


def test_codex_build_env_without_chat_id_does_not_invent_session(tmp_path):
  env = CodexProvider().build_env(
    base_env={},
    data_dir=str(tmp_path),
    chat_id=None,
  )

  assert env["CODEX_HOME"] == str(tmp_path / "cli-auth" / "codex")
  assert "AGENT_BROWSER_SESSION" not in env


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


# VIEWPORT_WIDTH/HEIGHT belong to the same agent-browser env contract:
# chat.py exports them per turn and agent-screenshot.sh hard-requires
# both (deliberately strict — fix producers, never the consumer).


def test_viewport_env_passes_through_the_shell_sent_viewport():
  env = viewport_env({"width": 390, "height": 844})
  assert env == {"VIEWPORT_WIDTH": "390", "VIEWPORT_HEIGHT": "844"}


def test_viewport_env_defaults_when_no_shell_sent_a_viewport():
  # Shell-less turns (cron, reflection, background continuations from
  # apps.py / platform_update.py) never send a viewport; the documented
  # default keeps screenshots working there instead of hard-failing.
  env = viewport_env(None)
  assert env == {
    "VIEWPORT_WIDTH": str(DEFAULT_VIEWPORT_WIDTH),
    "VIEWPORT_HEIGHT": str(DEFAULT_VIEWPORT_HEIGHT),
  }


def test_viewport_env_defaults_on_malformed_viewport():
  # A half-set or zero payload must not export a broken pair — the
  # helper requires BOTH values, so anything short of that defaults.
  for bad in ({}, {"width": 390}, {"height": 844}, {"width": 0, "height": 915}):
    env = viewport_env(bad)
    assert env["VIEWPORT_WIDTH"] == str(DEFAULT_VIEWPORT_WIDTH)
    assert env["VIEWPORT_HEIGHT"] == str(DEFAULT_VIEWPORT_HEIGHT)
