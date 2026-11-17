"""Unit tests for the pure structural-redaction functions (capability B).

These test the whitelist + secret-scrub directly, with no FastAPI /
DB / auth in the loop, so a redaction regression surfaces here even if
the route layer is fine.
"""

from app import chat_log_redaction as r


def test_redact_keeps_only_role_and_text_dropping_all_block_types():
  msg = {
    "role": "assistant",
    "content": "visible answer",
    "blocks": [
      {"type": "text", "content": "visible answer"},
      {"type": "tool", "tool": "Bash", "input": "secret cmd", "output": "out"},
      {"type": "thinking", "content": "private reasoning"},
      {"type": "error", "message": "boom"},
      {"type": "question", "questions": [{"question": "pick?"}]},
    ],
  }
  out = r.redact_message(msg)
  assert out == {"role": "assistant", "text": "visible answer"}


def test_redact_keeps_a_cards_closing_message_but_not_its_prompts():
  msg = {
    "role": "assistant",
    "content": "Done.\n\nThe fix is ready.",
    "blocks": [
      {"type": "text", "content": "Done."},
      {"type": "question", "message": "The fix is ready.",
       "questions": [{"question": "pick?", "options": [{"label": "secret"}]}]},
    ],
  }
  assert r.redact_message(msg) == {
    "role": "assistant", "text": "Done.\n\nThe fix is ready.",
  }


def test_redact_scrubs_a_secret_shaped_value_in_a_cards_closing_message():
  key = "sk-ant-api03-ABCDEFGHIJKLMNOP"
  out = r.redact_message({
    "role": "assistant",
    "content": f"Saved the key {key}.",
    "blocks": [{"type": "question", "message": f"Saved the key {key}.",
                "questions": [{"question": "Use it?"}]}],
  })
  assert out["role"] == "assistant"
  assert "Saved the key" in out["text"]
  assert "sk-ant-" not in out["text"]


def test_redact_separates_distinct_assistant_text_blocks():
  msg = {
    "role": "assistant",
    "content": "fallback",
    "blocks": [
      {"type": "text", "content": "First sentence."},
      {"type": "text", "content": "Second sentence."},
    ],
  }
  assert r.redact_message(msg) == {
    "role": "assistant",
    "text": "First sentence.\n\nSecond sentence.",
  }


def test_hidden_user_message_is_dropped_entirely():
  assert r.redact_message(
    {"role": "user", "content": "internal answer", "hidden": True}
  ) is None


def test_automatic_continuation_is_redacted_as_a_product_event():
  assert r.redact_message({
    "role": "user",
    "content": "continue",
    "kind": "auto_continuation",
    "continuation_reason": "restart",
  }) == {
    "role": "system",
    "text": "Automatic continuation (restart).",
  }


def test_pure_tool_turn_with_no_text_is_dropped():
  msg = {
    "role": "assistant",
    "content": "",
    "blocks": [{"type": "tool", "tool": "x", "input": "y", "output": "z"}],
  }
  assert r.redact_message(msg) is None


def test_strip_upload_augmentation_removes_fs_path_block():
  content = (
    "summarize this\n\n"
    "[Files in this session:\n"
    "- a.txt → /data/chats/c/a.txt (text/plain, 1 KB)\n"
    "- b.png → /data/chats/c/b.png (image/png, 9 KB)]"
  )
  stripped = r.strip_upload_augmentation(content)
  assert stripped == "summarize this"
  assert "/data/chats" not in stripped


def test_strip_upload_augmentation_handles_brackets_in_filenames():
  content = (
    "summarize this\n\n"
    "[Files in this session:\n"
    "- chart[final].png → /data/chats/c/chart[final].png "
    "(image/png, 9 KB)]"
  )
  assert r.strip_upload_augmentation(content) == "summarize this"


def test_scrub_secrets_catches_jwt_key_and_bearer_shapes():
  assert "eyJ" not in r.scrub_secrets(
    "tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.SIGNATUREXYZ here"
  )
  assert "sk-ant-" not in r.scrub_secrets("key sk-ant-api03-ABCDEFGHIJ tail")
  assert r.scrub_secrets("Authorization: Bearer abcdefABCDEF123456") \
    != "Authorization: Bearer abcdefABCDEF123456"
  assert "=" in r.scrub_secrets("api_key=hunter2hunter2hunter2")


def test_scrub_secrets_leaves_ordinary_prose_untouched():
  prose = "Buy milk, eggs, and bread on the way home tonight."
  assert r.scrub_secrets(prose) == prose


def test_redact_messages_caps_length_and_takes_newest_slice():
  msgs = [
    {"role": "user", "content": f"message number {i}"} for i in range(10)
  ]
  out = r.redact_messages(msgs, newest=3)
  assert len(out) == 3
  # newest slice → the last three messages, in order.
  assert out[-1]["text"] == "message number 9"

  long = [{"role": "user", "content": "x" * 5000}]
  capped = r.redact_messages(long, char_cap=100)
  assert len(capped[0]["text"]) <= 101  # 100 + ellipsis


def test_list_preview_count_matches_redacted_message_survivors():
  msgs = [
    {"role": "user", "content": "hi"},
    {"role": "user", "content": "secret", "hidden": True},
    {"role": "assistant", "content": "", "blocks": [
      {"type": "tool", "tool": "x", "input": "y", "output": "z"}]},
    {"role": "assistant", "content": "ok", "blocks": [
      {"type": "text", "content": "ok"}]},
  ]
  assert r.list_preview(msgs).message_count == 2
  assert len(r.redact_messages(msgs)) == 2


def test_list_preview_excerpt_is_the_first_survivor_and_walks_the_transcript_once():
  walked = []

  class OneShotTranscript:
    def __iter__(self):
      walked.append(True)
      yield {"role": "user", "content": "first", "hidden": True}
      yield {"role": "user", "content": "x" * 400}
      yield {"role": "assistant", "content": "later"}

  preview = r.list_preview(OneShotTranscript(), char_cap=10)
  assert preview == r.ListPreview(2, "x" * 10 + "…")
  assert walked == [True]
  assert r.list_preview([]) == r.ListPreview(0, "")
