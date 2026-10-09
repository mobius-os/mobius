"""Known handoff refusals are actionable without exposing provider data."""

from app import transcript_rows
import json

import pytest

from app import compaction, models


CASES = [
  ("Your workspace is out of credits. Add credits to continue.", "Add credits"),
  ("insufficient_quota", "Add credits"),
  ("insufficient_credits", "Add credits"),
  ("You've hit your weekly limit · resets Jan 1, 12am (UTC)", "allowance"),
  ("usage_limit_reached", "allowance"),
  ("rate_limit_exceeded", "allowance"),
  ("Too many requests", "allowance"),
  ("Invalid authentication credentials", "Reconnect"),
  ("authentication_failed", "Reconnect"),
  ("invalid_api_key", "Reconnect"),
  ("Unauthorized", "Reconnect"),
  ("not logged in", "Reconnect"),
]


@pytest.mark.parametrize("cause,action", CASES)
@pytest.mark.parametrize("channel", ["error", "turn.failed", "string-error", "stderr"])
def test_known_codex_refusals_explain_next_step_without_echoing_data(cause, action, channel):
  private_error = cause + " secret-credential-and-transcript"
  if channel == "stderr":
    stdout, stderr = b"", private_error.encode()
  else:
    stdout = json.dumps({
      "type": "error" if channel == "string-error" else channel,
      "error": private_error if channel == "string-error" else {"message": private_error},
    }).encode()
    stderr = b"secret-stderr"
  message = compaction._codex_compaction_failure(stdout, stderr)
  assert action in message
  assert "conversation is unchanged" in message
  assert "secret" not in message


@pytest.mark.parametrize("cause,action", CASES)
def test_wording_fits_switch_and_manual_compact(cause, action):
  # The summarizer also serves manual /compact, and app-provided or
  # subscription providers are not reconnected in Settings.
  message = compaction._provider_compaction_failure(cause)
  assert action in message
  assert "switch" not in message.lower()
  assert "Settings" not in message


@pytest.mark.parametrize("cause,action", CASES)
def test_assistant_prose_is_not_failure_evidence(cause, action):
  stdout = json.dumps({"type": "agent_message", "text": cause}).encode()
  assert compaction._codex_compaction_failure(stdout, b"") == (
    "The incoming provider could not compact the chat."
  )


def test_structured_failure_takes_precedence_over_unrelated_stderr():
  stdout = b'{"type":"turn.failed","error":{"message":"unknown failure"}}'
  assert compaction._codex_compaction_failure(stdout, b"Unauthorized secret") == (
    "The incoming provider could not compact the chat."
  )


@pytest.mark.parametrize("cause,action", CASES)
def test_known_refusal_reaches_switch_response_without_changing_chat(
  client, auth, db, monkeypatch, cause, action,
):
  monkeypatch.setattr("app.providers.CodexProvider.check_auth", lambda *_args: None)

  async def refuse(_messages, **_kwargs):
    stdout = json.dumps({"type": "error", "message": cause}).encode()
    raise compaction.CompactionError(compaction._codex_compaction_failure(stdout, b""))

  monkeypatch.setattr(compaction, "summarize_chat", refuse)
  chat_id = client.post("/api/chats", json={"title": "Preserve me"}, headers=auth).json()["id"]
  source = [
    {"role": "user", "content": "Keep my history"},
    {"role": "assistant", "content": "Original answer"},
  ]
  client.put(f"/api/chats/{chat_id}", json={"messages": source}, headers=auth)
  row = db.get(models.Chat, chat_id)
  row.provider = "claude"
  row.session_id = "original-session"
  row.agent_settings_json = {"model": "claude-sonnet-4-6"}
  before = transcript_rows.read_all(db, row)
  db.commit()

  response = client.post(
    f"/api/chats/{chat_id}/provider-switch", headers=auth, json={
      "switch_id": "known-refusal", "provider": "codex",
      "agent_settings_json": {"model": "gpt-5.4", "effort": "high"},
    },
  )
  assert response.status_code == 422
  assert action in response.json()["detail"]
  db.expire_all()
  row = db.get(models.Chat, chat_id)
  assert row.provider == "claude"
  assert row.session_id == "original-session"
  assert row.agent_settings_json == {"model": "claude-sonnet-4-6"}
  assert transcript_rows.read_all(db, row) == before


CLAUDE_TEXT_CASES = [
  ("Your workspace is out of credits.", "Add credits"),
  ("You've hit your weekly limit", "allowance"),
  ("Invalid authentication credentials", "Reconnect"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,cause,error_type,status,action", [
  *[("result", cause, None, None, action) for cause, action in CLAUDE_TEXT_CASES],
  *[("errors", cause, None, None, action) for cause, action in CLAUDE_TEXT_CASES],
  *[("assistant-error", cause, "unknown", None, action)
    for cause, action in CLAUDE_TEXT_CASES],
  # Claude's own error types classify without relying on message wording.
  ("assistant-error", "unrelated wording", "billing_error", None, "Add credits"),
  ("assistant-error", "unrelated wording", "rate_limit", None, "allowance"),
  ("assistant-error", "unrelated wording", "authentication_failed", None, "Reconnect"),
  # HTTP status alone is enough, with no text at all.
  ("status", None, None, 429, "allowance"),
  ("status", None, None, 401, "Reconnect"),
])
async def test_claude_error_terminal_is_actionable_and_discards_partial_text(
  monkeypatch, tmp_path, source, cause, error_type, status, action,
):
  from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

  private = f"{cause} secret-provider-data"
  terminal = ResultMessage(
    subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
    is_error=True, num_turns=1, session_id="private-session",
    result=private if source == "result" else None,
    errors=[private] if source == "errors" else None,
    api_error_status=status,
  )

  class Provider:
    def build_env(self, **_kwargs):
      return {}

  class Client:
    disconnected = False

    async def connect(self):
      pass

    async def query(self, _prompt):
      pass

    async def receive_response(self):
      yield AssistantMessage(
        content=[TextBlock(text=cause if source == "assistant-error" else "partial-secret")],
        model="claude", error=error_type,
      )
      yield terminal

    async def disconnect(self):
      self.disconnected = True

  client = Client()
  monkeypatch.setattr("app.providers.get_provider", lambda _pid: Provider())
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: client)
  with pytest.raises(compaction.CompactionError) as failure:
    await compaction._run_claude_summarize_turn(
      "private prompt", data_dir=str(tmp_path), provider_id="claude", model=None, effort=None,
    )
  assert action in str(failure.value)
  assert "secret" not in str(failure.value)
  assert client.disconnected


def _events(*events):
  return "\n".join(json.dumps(event) for event in events).encode()


def test_codex_retried_rate_limit_does_not_mask_final_sign_in_failure():
  stdout = _events(
    {"type": "error", "message": "429 Too Many Requests; retrying"},
    {"type": "error", "message": "Unauthorized"},
    {"type": "turn.failed", "error": {"message": "Unauthorized"}},
  )
  assert "Reconnect" in compaction._codex_compaction_failure(stdout, b"")


def test_codex_final_turn_failure_wins_over_later_error_event():
  stdout = _events(
    {"type": "turn.failed", "error": {"message": "invalid_api_key"}},
    {"type": "error", "message": "rate limit exceeded"},
  )
  assert "Reconnect" in compaction._codex_compaction_failure(stdout, b"")


def test_codex_last_error_event_classifies_without_turn_failure():
  stdout = _events(
    {"type": "error", "message": "rate limit exceeded; retrying"},
    {"type": "error", "message": "insufficient_quota"},
  )
  assert "Add credits" in compaction._codex_compaction_failure(stdout, b"")


@pytest.mark.parametrize("cause,action", [
  # Möbius subscription refusal that live Codex turns already explain.
  ("not enough credits for the maximum request cost", "Add credits"),
  # Limit forms the live-turn rules already parked on.
  ("model overloaded, try again", "allowance"),
  ("HTTP 429", "allowance"),
  ("You've hit your limit · resets 5pm", "allowance"),
])
def test_compaction_shares_live_turn_rules(cause, action):
  assert action in compaction._provider_compaction_failure(cause)


def test_compaction_reads_exhausted_workspace_credits_as_credits_not_limit():
  # Live turns pause on this refusal even when Codex also reports a 429.
  message = compaction._provider_compaction_failure(
    "Your workspace is out of credits. Add credits to continue.", status=429,
  )
  assert "Add credits" in message


@pytest.mark.asyncio
async def test_claude_retried_rate_limit_does_not_mask_final_sign_in_failure(
  monkeypatch, tmp_path,
):
  from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

  class Provider:
    def build_env(self, **_kwargs):
      return {}

  class Client:
    async def connect(self):
      pass

    async def query(self, _prompt):
      pass

    async def receive_response(self):
      yield AssistantMessage(
        content=[TextBlock(text="Rate limited; retrying")],
        model="claude", error="rate_limit",
      )
      yield AssistantMessage(
        content=[TextBlock(text="Invalid authentication credentials")],
        model="claude", error="unknown",
      )
      yield ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="s",
      )

    async def disconnect(self):
      pass

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: Provider())
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: Client())
  with pytest.raises(compaction.CompactionError) as failure:
    await compaction._run_claude_summarize_turn(
      "prompt", data_dir=str(tmp_path), provider_id="claude", model=None, effort=None,
    )
  assert "Reconnect" in str(failure.value)


@pytest.mark.asyncio
async def test_claude_retried_rate_limit_does_not_mask_terminal_result_text(
  monkeypatch, tmp_path,
):
  from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

  class Provider:
    def build_env(self, **_kwargs):
      return {}

  class Client:
    async def connect(self):
      pass

    async def query(self, _prompt):
      pass

    async def receive_response(self):
      yield AssistantMessage(
        content=[TextBlock(text="Rate limited; retrying")],
        model="claude", error="rate_limit",
      )
      yield AssistantMessage(content=[TextBlock(text="partial")], model="claude")
      yield ResultMessage(
        subtype="error_during_execution", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="s",
        result="Invalid authentication credentials",
      )

    async def disconnect(self):
      pass

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: Provider())
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: Client())
  with pytest.raises(compaction.CompactionError) as failure:
    await compaction._run_claude_summarize_turn(
      "prompt", data_dir=str(tmp_path), provider_id="claude", model=None, effort=None,
    )
  assert "Reconnect" in str(failure.value)


@pytest.mark.parametrize(("text", "status"), [
  ("Login expired \u00b7 Please run /login", None),
  (
    "Your account does not have access to Claude. Please login again or "
    "contact your administrator.",
    403,
  ),
  ("Failed to authenticate: OAuth session expired. Please run /login.", None),
])
@pytest.mark.asyncio
async def test_claude_auth_error_type_survives_repeated_result_text(
  monkeypatch, tmp_path, text, status,
):
  # The CLI repeats the API error text in the terminal result; the structured
  # error type must still classify wording that lacks sign-in keywords.
  from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

  class Provider:
    def build_env(self, **_kwargs):
      return {}

  class Client:
    async def connect(self):
      pass

    async def query(self, _prompt):
      pass

    async def receive_response(self):
      yield AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude", error="authentication_failed",
      )
      yield ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1,
        is_error=True, num_turns=1, session_id="s",
        result=text, api_error_status=status,
      )

    async def disconnect(self):
      pass

  monkeypatch.setattr("app.providers.get_provider", lambda _pid: Provider())
  monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", lambda _opts: Client())
  with pytest.raises(compaction.CompactionError) as failure:
    await compaction._run_claude_summarize_turn(
      "prompt", data_dir=str(tmp_path), provider_id="claude", model=None, effort=None,
    )
  assert "Reconnect" in str(failure.value)
