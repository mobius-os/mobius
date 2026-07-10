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
